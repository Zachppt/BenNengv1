# ============================================================
# main.py — v3
# 相对强度过滤 + 概率制推送
# ============================================================

import asyncio
import aiohttp
import json
import os
import sys
import time
import logging
import statistics

from config import (
    SCAN_INTERVAL_MINUTES, COLDSTART_SNAPSHOTS,
    FILTER, ALERT_PROBABILITY, DEDUP_MINUTES,
    NOISE_THRESHOLD, RELATIVE_STRENGTH_THRESHOLD,
    SCAN_RESULT_PATH, ALERT_STATE_PATH, SHARED_DIR,
)
from data.fetcher import fetch_all_prices, fetch_token_full, fetch_token_realtime
from cache.snapshot import (
    init_db, build_exchange_snapshot, save_snapshot_batch,
    save_phase, save_alert, can_push, record_push,
    get_snapshots, is_coldstart_done,
)
from rules.engine import RuleEngine, aggregate, prob_to_level
from alerts.telegram import (
    send, send_photo, fmt_high_alert, fmt_medium_batch,
    fmt_scan_summary, fmt_system,
)
from alerts.chart_generator import generate_kline_chart

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scanner.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("main")

engine       = RuleEngine()
hfreq_tokens: set = set()
START_TIME   = time.time()
_last_baseline_update = 0


# ============================================================
# 共享存储
# ============================================================

def ensure_shared_dir():
    os.makedirs(SHARED_DIR, exist_ok=True)


def write_scan_result(data: dict):
    ensure_shared_dir()
    with open(SCAN_RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def read_alert_state() -> dict:
    ensure_shared_dir()
    if os.path.exists(ALERT_STATE_PATH):
        with open(ALERT_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"muted": {}, "hfreq_tokens": [], "updated_ts": 0}


def write_alert_state(state: dict):
    state["updated_ts"] = int(time.time())
    with open(ALERT_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def is_muted(token: str) -> bool:
    state = read_alert_state()
    return time.time() < state.get("muted", {}).get(token, 0)


# ============================================================
# 第一层过滤（加入相对BTC超额涨幅）
# ============================================================

async def layer1_filter(session: aiohttp.ClientSession) -> list:
    logger.info("第一层过滤：拉取全市场价格...")
    all_prices = await fetch_all_prices(session)
    if not all_prices:
        logger.error("全市场价格拉取失败")
        return []

    # 获取BTC 4h变化（作为市场基准）
    btc_change = 0
    btc_snaps  = get_snapshots("BTC", "binance", limit=17)
    if len(btc_snaps) >= 16:
        btc_old = btc_snaps[-1].get("price", 0)
        btc_now = all_prices.get("BTCUSDT", 0)
        if btc_old > 0 and btc_now > 0:
            btc_change = (btc_now - btc_old) / btc_old

    candidates = []

    for symbol, price in all_prices.items():
        if not symbol.endswith("USDT"):
            continue
        token = symbol.replace("USDT", "")

        if token in hfreq_tokens:
            candidates.append({"token": token, "price": price,
                                "reason": "高频监控"})
            continue

        snaps = get_snapshots(token, "binance", limit=17)
        if len(snaps) >= 16:
            old_price = snaps[-1].get("price", 0)
            if old_price > 0:
                change = (price - old_price) / old_price

                # 绝对涨幅过滤
                if abs(change) > FILTER["price_change_4h"]:
                    # 相对BTC超额涨幅
                    excess = change - btc_change
                    candidates.append({
                        "token":      token,
                        "price":      price,
                        "change":     change,
                        "excess":     excess,
                        "reason":     f"4h{change*100:+.1f}% (超额{excess*100:+.1f}%)",
                    })
        else:
            candidates.append({"token": token, "price": price,
                                "reason": "新代币"})

    logger.info(f"第一层过滤：{len(all_prices)}个合约 → {len(candidates)}个候选")
    return candidates


# ============================================================
# 第二层：单币采集 + 规则计算
# ============================================================

async def process_token(session: aiohttp.ClientSession,
                         token: str) -> dict | None:
    try:
        raw = await fetch_token_full(session, token)
        if not raw:
            return None

        agg = aggregate(token, raw)

        # 存快照
        exchange_data = build_exchange_snapshot(token, raw, agg)
        save_snapshot_batch(token, exchange_data)

        cold_done = is_coldstart_done(token, COLDSTART_SNAPSHOTS)
        result    = engine.run(
            token, agg,
            snapshot_fn=get_snapshots,
            coldstart_done=cold_done,
        )

        if result["probability"] >= ALERT_PROBABILITY["WATCH"]:
            save_phase(token, result["phase"])
            save_alert(token, result["score"], result["level"],
                       result["phase"], result["triggered"])

        return result

    except Exception as e:
        logger.warning(f"{token} 处理失败: {e}")
        return None


# ============================================================
# 相对强度过滤（核心新增）
# ============================================================

def relative_strength_filter(all_results: list) -> list:
    """
    只推送明显超出市场均值的代币
    解决"全市场同时触发"的误报问题
    """
    if not all_results:
        return []

    probs = [r["probability"] for r in all_results]
    market_avg = statistics.mean(probs)

    logger.info(f"市场平均上涨概率：{market_avg*100:.1f}%")

    filtered = []
    for r in all_results:
        prob      = r["probability"]
        relative  = prob / market_avg if market_avg > 0 else 1

        # 必须超出市场均值1.5倍 或 绝对概率>75%
        if (relative >= RELATIVE_STRENGTH_THRESHOLD
                or prob >= ALERT_PROBABILITY["HIGH"]):
            filtered.append({**r, "market_avg": market_avg,
                              "relative_strength": relative})
        elif prob >= ALERT_PROBABILITY["MEDIUM"]:
            # MEDIUM只要超出均值1.2倍
            if relative >= 1.2:
                filtered.append({**r, "market_avg": market_avg,
                                  "relative_strength": relative})

    logger.info(
        f"相对强度过滤：{len(all_results)}个 → {len(filtered)}个"
        f"（均值{market_avg*100:.1f}%，阈值{RELATIVE_STRENGTH_THRESHOLD}x）"
    )
    return filtered, market_avg


# ============================================================
# 噪音过滤（保留，提高阈值）
# ============================================================

def noise_filter(all_results: list) -> list:
    from collections import Counter
    rule_counts = Counter(
        r["rule"]
        for res in all_results
        for r in res.get("triggered", [])
    )
    noise_rules = {
        rule for rule, cnt in rule_counts.items()
        if cnt > NOISE_THRESHOLD   # 已在config.py调高到40
    }
    if noise_rules:
        logger.info(f"噪音规则过滤({len(noise_rules)}条): {list(noise_rules)[:5]}...")

    filtered = []
    for res in all_results:
        clean = [r for r in res["triggered"]
                 if r["rule"] not in noise_rules]
        from rules.engine import calc_probability, prob_to_level
        prob  = calc_probability(clean)
        level = prob_to_level(prob)
        filtered.append({**res, "triggered": clean,
                         "probability": prob, "level": level})
    return filtered


# ============================================================
# 单次完整扫描
# ============================================================

async def run_scan():
    scan_start = time.time()
    logger.info("=" * 55)
    logger.info("开始扫描...")

    async with aiohttp.ClientSession() as session:

        candidates = await layer1_filter(session)
        if not candidates:
            logger.warning("候选池为空，跳过")
            return

        # 并发采集（每批20个）
        logger.info(f"第二层深度采集：{len(candidates)}个代币...")
        all_results = []
        BATCH = 20

        for i in range(0, len(candidates), BATCH):
            batch   = candidates[i:i+BATCH]
            tasks   = [process_token(session, c["token"]) for c in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for res in results:
                if isinstance(res, dict) and res:
                    prob = res.get("probability", 0)
                    if prob >= ALERT_PROBABILITY["WATCH"]:
                        all_results.append(res)
                        logger.info(
                            f"{res['token']}: {prob*100:.0f}% "
                            f"{res['level']} {res['phase']}"
                        )

            if i + BATCH < len(candidates):
                await asyncio.sleep(1.0)   # 增加间隔，减少418

        # 噪音过滤
        all_results = noise_filter(all_results)

        # 相对强度过滤
        filtered_result = relative_strength_filter(all_results)
        if isinstance(filtered_result, tuple):
            filtered, market_avg = filtered_result
        else:
            filtered, market_avg = filtered_result, 0

        # 分级
        high_results   = [r for r in filtered
                          if r["probability"] >= ALERT_PROBABILITY["HIGH"]]
        medium_results = [r for r in filtered
                          if ALERT_PROBABILITY["MEDIUM"] <= r["probability"]
                          < ALERT_PROBABILITY["HIGH"]]

        # 更新高频监控列表
        hfreq_tokens.clear()
        for r in filtered:
            if r["probability"] >= ALERT_PROBABILITY["MEDIUM"]:
                hfreq_tokens.add(r["token"])

        # 写共享存储
        write_scan_result({
            "scan_ts":      int(time.time()),
            "duration_sec": time.time() - scan_start,
            "total_scanned":len(candidates),
            "market_avg_prob": market_avg,
            "high_alerts": [
                {"token": r["token"], "probability": r["probability"],
                 "level": r["level"], "phase": r["phase"],
                 "triggered": r["triggered"][:8], "pushed": False}
                for r in high_results
            ],
            "medium_alerts": [
                {"token": r["token"], "probability": r["probability"],
                 "phase": r["phase"], "pushed": False}
                for r in medium_results
            ],
            "system": {
                "coldstart_done": is_coldstart_done(
                    candidates[0]["token"] if candidates else "BTC",
                    COLDSTART_SNAPSHOTS),
                "hfreq_tokens": list(hfreq_tokens),
                "next_scan_ts": int(time.time() + SCAN_INTERVAL_MINUTES * 60),
            }
        })

        # ── 推送 HIGH（含K线图）──
        for r in sorted(high_results,
                         key=lambda x: x["probability"], reverse=True):
            token = r["token"]
            prob  = r["probability"]

            if is_muted(token):
                continue
            if not can_push(token, "HIGH", int(prob*100),
                            DEDUP_MINUTES["HIGH"]):
                continue

            # 先推送文字预警
            first_ago = _calc_first_alert_ago(token)
            msg = fmt_high_alert(r, first_ago)
            ok  = await send(msg)

            # 再推送K线图
            ka = r["agg"].get("kline_analysis", {}).get("binance", {})
            klines = (r["agg"].get("raw", {}).get("binance", {})
                      .get("futures", {}).get("klines") or [])
            if klines and ka:
                ka["probability"] = prob
                chart_bytes = generate_kline_chart(
                    token, klines, ka, r.get("entry_zone", {})
                )
                if chart_bytes:
                    await send_photo(chart_bytes,
                                     caption=f"{token}/USDT K线图")

            if ok:
                record_push(token, "HIGH", int(prob*100))
                logger.info(f"✅ 推送HIGH: {token} {prob*100:.0f}%")

        # ── 推送 MEDIUM（批量）──
        medium_to_push = [
            r for r in sorted(medium_results,
                               key=lambda x: x["probability"], reverse=True)
            if (not is_muted(r["token"])
                and can_push(r["token"], "MEDIUM",
                             int(r["probability"]*100),
                             DEDUP_MINUTES["MEDIUM"]))
        ]
        if medium_to_push:
            msg = fmt_medium_batch(medium_to_push)
            ok  = await send(msg)
            if ok:
                for r in medium_to_push:
                    record_push(r["token"], "MEDIUM",
                                int(r["probability"]*100))

        # ── 扫描总结 ──
        elapsed = time.time() - scan_start
        cold_tokens = [
            c["token"] for c in candidates
            if not is_coldstart_done(c["token"], COLDSTART_SNAPSHOTS)
        ]
        summary = fmt_scan_summary(
            high=len(high_results),
            medium=len(medium_results),
            total=len(candidates),
            elapsed=elapsed,
            next_min=SCAN_INTERVAL_MINUTES,
            coldstart_tokens=cold_tokens[:3] if cold_tokens else None,
            market_avg_prob=market_avg,
        )
        await send(summary)

        logger.info(
            f"扫描完成：{elapsed:.1f}秒 "
            f"HIGH={len(high_results)} MEDIUM={len(medium_results)} "
            f"市场均值={market_avg*100:.1f}%"
        )


def _calc_first_alert_ago(token: str) -> int:
    from cache.snapshot import get_alert_history
    history = get_alert_history(token, hours=24)
    if not history:
        return 0
    oldest_ts = min(h["ts"] for h in history)
    return int((time.time() * 1000 - oldest_ts) / 60000)


# ============================================================
# CLI 入口
# ============================================================

async def main():
    ensure_shared_dir()
    init_db()

    args = sys.argv[1:]

    if "--once" in args:
        await run_scan()

    elif "--heartbeat" in args:
        state = read_alert_state()
        hfreq = state.get("hfreq_tokens", [])
        await send(fmt_system(
            "heartbeat",
            f"高频监控: {', '.join(hfreq) if hfreq else '无'}"
        ))

    else:
        logger.info("上涨预警系统启动（持续运行模式）")
        await send(fmt_system(
            "startup",
            f"扫描间隔 {SCAN_INTERVAL_MINUTES} 分钟\n"
            f"相对强度过滤：市场均值 {RELATIVE_STRENGTH_THRESHOLD}x\n"
            f"噪音阈值：{NOISE_THRESHOLD} 个代币"
        ))

        while True:
            try:
                await run_scan()
            except Exception as e:
                logger.exception(f"扫描异常: {e}")
                await send(fmt_system("error", str(e)))
            await asyncio.sleep(SCAN_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    asyncio.run(main())
