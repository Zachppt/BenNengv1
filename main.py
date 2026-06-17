# ============================================================
# main.py — v4
# 双向预警 + 行为分类 + 每日汇总 + 自动记录
# ============================================================

import asyncio
import aiohttp
import json
import os
import sys
import time
import logging
import statistics
from datetime import datetime, timezone

from config import (
    SCAN_INTERVAL_MINUTES, COLDSTART_SNAPSHOTS,
    FILTER, ALERT_PROBABILITY, DEDUP_MINUTES,
    NOISE_THRESHOLD, RELATIVE_STRENGTH_THRESHOLD,
    SCAN_RESULT_PATH, ALERT_STATE_PATH, SHARED_DIR,
)
from data.fetcher import (
    fetch_all_prices, fetch_token_full, fetch_token_realtime
)
from cache.snapshot import (
    init_db, build_exchange_snapshot, save_snapshot_batch,
    save_phase, save_alert, can_push, record_push,
    get_snapshots, is_coldstart_done,
)
from rules.engine import RuleEngine, aggregate
from rules.market_context import get_market_context
from alerts.telegram import (
    send, send_photo,
    fmt_long_alert, fmt_short_alert,
    fmt_medium_batch, fmt_daily_summary, fmt_system,
)
from alerts.chart_generator import generate_kline_chart
from backtest.recorder import (
    init_backtest_tables, record_alert,
    evaluate_pending_alerts, calc_daily_stats,
)
from paper_trading.account import init_paper_trading_tables

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scanner.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger("main")

engine        = RuleEngine()
hfreq_tokens: set = set()
START_TIME    = time.time()
_last_daily_summary = 0


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
# 第一层过滤（含BTC超额涨幅）
# ============================================================

async def layer1_filter(session) -> list:
    logger.info("第一层过滤：拉取全市场价格...")
    all_prices = await fetch_all_prices(session)
    if not all_prices:
        logger.error("全市场价格拉取失败")
        return []

    # BTC 4h 基准
    btc_change = 0
    btc_snaps  = get_snapshots("BTC", "binance", limit=17)
    btc_now    = all_prices.get("BTCUSDT", 0)
    if len(btc_snaps) >= 16 and btc_now > 0:
        btc_old = btc_snaps[-1].get("price", 0)
        if btc_old > 0:
            btc_change = (btc_now - btc_old) / btc_old

    candidates = []

    for symbol, price in all_prices.items():
        if not symbol.endswith("USDT"):
            continue
        token = symbol.replace("USDT", "")

        if token in hfreq_tokens:
            candidates.append({"token": token, "price": price,
                                "change": 0, "excess": 0,
                                "reason": "高频监控"})
            continue

        snaps = get_snapshots(token, "binance", limit=17)
        if len(snaps) >= 16:
            old = snaps[-1].get("price", 0)
            if old > 0:
                change = (price - old) / old
                excess = change - btc_change

                # 绝对涨幅过滤
                if abs(change) > FILTER["price_change_4h"]:
                    candidates.append({
                        "token":  token,
                        "price":  price,
                        "change": change,
                        "excess": excess,
                        "reason": f"4h{change*100:+.1f}% 超额{excess*100:+.1f}%",
                    })
        else:
            candidates.append({"token": token, "price": price,
                                "change": 0, "excess": 0,
                                "reason": "新代币"})

    logger.info(
        f"第一层过滤：{len(all_prices)}个合约 → {len(candidates)}个候选 "
        f"BTC基准{btc_change*100:+.1f}%"
    )
    return candidates, btc_change


# ============================================================
# 第二层：单币采集 + 规则计算
# ============================================================

async def process_token(session, token: str,
                         token_change: float,
                         btc_change: float,
                         all_partial_results: list) -> dict | None:
    try:
        raw = await fetch_token_full(session, token)
        if not raw:
            return None

        agg = aggregate(token, raw)

        # 快照存储
        exchange_data = build_exchange_snapshot(token, raw, agg)
        save_snapshot_batch(token, exchange_data)

        # 市场背景
        market_ctx = await get_market_context(
            session, token, token_change,
            get_snapshots, all_partial_results
        )

        cold_done = is_coldstart_done(token, COLDSTART_SNAPSHOTS)
        result    = engine.run(
            token, agg,
            snapshot_fn=get_snapshots,
            coldstart_done=cold_done,
            market_context=market_ctx,
        )

        # 记录阶段
        if result["probability"] >= ALERT_PROBABILITY["WATCH"]:
            save_phase(token, result["phase"])
            save_alert(
                token, result["score"], result["level"],
                result["phase"], result["triggered"]
            )

        return result

    except Exception as e:
        logger.warning(f"{token} 处理失败: {e}")
        return None


# ============================================================
# 相对强度过滤（双向）
# ============================================================

def relative_strength_filter(all_results: list) -> tuple:
    if not all_results:
        return [], 0

    # 按方向分别计算市场均值
    long_probs  = [r["long_probability"]  for r in all_results]
    short_probs = [r["short_probability"] for r in all_results]

    market_long_avg  = statistics.mean(long_probs)  if long_probs  else 0.5
    market_short_avg = statistics.mean(short_probs) if short_probs else 0.5
    market_avg       = max(market_long_avg, market_short_avg)

    logger.info(
        f"市场均值 做多{market_long_avg*100:.1f}% "
        f"做空{market_short_avg*100:.1f}%"
    )

    filtered = []
    for r in all_results:
        lp = r["long_probability"]
        sp = r["short_probability"]
        d  = r["direction"]

        # 被动跟随直接过滤
        if r.get("classification", {}).get("behavior_type") == "REACTIVE":
            continue

        if d == "LONG":
            rel = lp / market_long_avg if market_long_avg > 0 else 1
            if (rel >= RELATIVE_STRENGTH_THRESHOLD
                    or lp >= ALERT_PROBABILITY["HIGH"]):
                filtered.append({**r, "market_avg": market_long_avg,
                                   "relative_strength": rel})
            elif lp >= ALERT_PROBABILITY["MEDIUM"] and rel >= 1.2:
                filtered.append({**r, "market_avg": market_long_avg,
                                   "relative_strength": rel})

        elif d == "SHORT":
            rel = sp / market_short_avg if market_short_avg > 0 else 1
            if (rel >= RELATIVE_STRENGTH_THRESHOLD
                    or sp >= ALERT_PROBABILITY["HIGH"]):
                filtered.append({**r, "market_avg": market_short_avg,
                                   "relative_strength": rel})
            elif sp >= ALERT_PROBABILITY["MEDIUM"] and rel >= 1.2:
                filtered.append({**r, "market_avg": market_short_avg,
                                   "relative_strength": rel})

    logger.info(
        f"相对强度过滤：{len(all_results)}个 → {len(filtered)}个"
    )
    return filtered, market_avg


# ============================================================
# 噪音过滤
# ============================================================

def noise_filter(all_results: list) -> list:
    from collections import Counter
    rule_counts = Counter(
        r["rule"]
        for res in all_results
        for r in (res.get("long_triggered", []) +
                  res.get("short_triggered", []))
    )
    noise_rules = {
        rule for rule, cnt in rule_counts.items()
        if cnt > NOISE_THRESHOLD
    }
    if noise_rules:
        logger.info(f"噪音过滤({len(noise_rules)}条规则)")

    filtered = []
    for res in all_results:
        clean_long  = [r for r in res.get("long_triggered", [])
                       if r["rule"] not in noise_rules]
        clean_short = [r for r in res.get("short_triggered", [])
                       if r["rule"] not in noise_rules]

        from rules.engine import (
            calc_long_probability, calc_short_probability,
            resolve_direction, prob_to_level
        )
        lp = calc_long_probability(clean_long)
        sp = calc_short_probability(clean_short)
        d  = resolve_direction(lp, sp)
        p  = lp if d == "LONG" else sp if d == "SHORT" else max(lp, sp)

        filtered.append({
            **res,
            "long_triggered":  clean_long,
            "short_triggered": clean_short,
            "long_probability": lp,
            "short_probability": sp,
            "probability": p,
            "direction": d,
            "level": prob_to_level(p),
        })

    return filtered


# ============================================================
# 每日汇总检查
# ============================================================

async def check_daily_summary():
    global _last_daily_summary

    now_utc = datetime.now(timezone.utc)
    # 00:00 UTC 推送
    if (now_utc.hour == 0 and now_utc.minute < 20
            and time.time() - _last_daily_summary > 3600):
        stats = calc_daily_stats()
        msg   = fmt_daily_summary(stats)
        await send(msg)
        _last_daily_summary = time.time()
        logger.info("每日汇报已推送")


# ============================================================
# 获取当前价格（供Paper Trading使用）
# ============================================================

async def _get_current_price(session, symbol: str) -> float:
    try:
        from data.fetcher import BinanceFuturesFetcher
        bf = BinanceFuturesFetcher()
        bt = await bf.book_ticker(session, symbol)
        return bt.get("mid", 0) if bt else 0
    except Exception:
        return 0


# ============================================================
# 单次完整扫描
# ============================================================

async def run_scan():
    scan_start = time.time()
    logger.info("=" * 55)
    logger.info("开始扫描...")

    async with aiohttp.ClientSession() as session:

        # 每日汇总检查
        await check_daily_summary()

        # 评估待定的 backtest 记录
        async def _fetch_price(sess, symbol):
            return await _get_current_price(sess, symbol)

        asyncio.create_task(
            evaluate_pending_alerts(session, _fetch_price)
        )

        # 第一层过滤
        filter_result = await layer1_filter(session)
        if isinstance(filter_result, tuple):
            candidates, btc_change = filter_result
        else:
            candidates, btc_change = filter_result, 0

        if not candidates:
            logger.warning("候选池为空，跳过")
            return

        # 第二层：并发采集
        logger.info(f"第二层深度采集：{len(candidates)}个代币...")
        all_results  = []
        partial      = []
        BATCH        = 15    # v4降低批次大小，减少418

        for i in range(0, len(candidates), BATCH):
            batch   = candidates[i:i+BATCH]
            tasks   = [
                process_token(
                    session, c["token"],
                    c.get("change", 0),
                    btc_change,
                    partial,
                )
                for c in batch
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for res in results:
                if isinstance(res, dict) and res:
                    prob = res.get("probability", 0)
                    if prob >= ALERT_PROBABILITY["WATCH"]:
                        all_results.append(res)
                        partial.append(res)
                        logger.info(
                            f"{res['token']}: "
                            f"多{res['long_probability']*100:.0f}% "
                            f"空{res['short_probability']*100:.0f}% "
                            f"{res['direction']} {res['phase']}"
                        )

            if i + BATCH < len(candidates):
                await asyncio.sleep(1.5)   # 增加间隔减少418

        # 噪音过滤
        all_results = noise_filter(all_results)

        # 相对强度过滤
        filtered, market_avg = relative_strength_filter(all_results)

        # 分级
        high_results   = [r for r in filtered
                          if r["probability"] >= ALERT_PROBABILITY["HIGH"]]
        medium_results = [r for r in filtered
                          if (ALERT_PROBABILITY["MEDIUM"]
                              <= r["probability"]
                              < ALERT_PROBABILITY["HIGH"])]

        # 更新高频监控列表
        hfreq_tokens.clear()
        for r in filtered:
            if r["probability"] >= ALERT_PROBABILITY["MEDIUM"]:
                hfreq_tokens.add(r["token"])

        # 写共享存储
        write_scan_result({
            "scan_ts":       int(time.time()),
            "duration_sec":  time.time() - scan_start,
            "total_scanned": len(candidates),
            "market_avg_prob": market_avg,
            "btc_change_4h": btc_change,
            "high_alerts": [
                {
                    "token":       r["token"],
                    "direction":   r["direction"],
                    "long_prob":   r["long_probability"],
                    "short_prob":  r["short_probability"],
                    "level":       r["level"],
                    "phase":       r["phase"],
                    "behavior":    r.get("classification", {}).get("behavior_label"),
                    "triggered":   r.get("long_triggered" if r["direction"] == "LONG"
                                         else "short_triggered", [])[:5],
                    "pushed":      False,
                }
                for r in high_results
            ],
            "medium_alerts": [
                {
                    "token":     r["token"],
                    "direction": r["direction"],
                    "long_prob": r["long_probability"],
                    "short_prob":r["short_probability"],
                    "phase":     r["phase"],
                    "pushed":    False,
                }
                for r in medium_results
            ],
            "system": {
                "coldstart_done": is_coldstart_done(
                    candidates[0]["token"] if candidates else "BTC",
                    COLDSTART_SNAPSHOTS),
                "hfreq_tokens": list(hfreq_tokens),
                "next_scan_ts": int(
                    time.time() + SCAN_INTERVAL_MINUTES * 60),
            }
        })

        # ── 推送 HIGH ALERT ──
        for r in sorted(high_results,
                         key=lambda x: x["probability"], reverse=True):
            token     = r["token"]
            prob      = r["probability"]
            direction = r["direction"]

            if is_muted(token):
                continue
            if not can_push(token, "HIGH", int(prob*100),
                            DEDUP_MINUTES["HIGH"]):
                continue

            # 计算首次预警以来的涨跌幅
            first_ago, price_change = _get_alert_context(token)

            # 格式化预警
            if direction == "LONG":
                msg = fmt_long_alert(r, first_ago, price_change)
            elif direction == "SHORT":
                msg = fmt_short_alert(r, first_ago, price_change)
            else:
                continue   # NEUTRAL 不推送

            ok = await send(msg)

            # K线图
            if ok:
                klines = (r["agg"].get("raw", {})
                          .get("binance", {})
                          .get("futures", {})
                          .get("klines") or [])
                ka     = r["agg"].get("kline_analysis", {}).get("binance", {})
                if klines and ka:
                    chart = generate_kline_chart(
                        token, klines, ka, r.get("entry_zone", {})
                    )
                    if chart:
                        await send_photo(chart)

                # 记录到 backtest
                entry  = r.get("entry_zone", {})
                fp     = r["agg"].get("futures_prices", {})
                ep     = fp.get("binance", 0) or (
                    sum(fp.values())/len(fp) if fp else 0
                )
                record_alert(
                    token=token,
                    direction=direction,
                    probability=prob,
                    phase=r["phase"],
                    behavior_type=r.get("classification", {}).get(
                        "behavior_type", "UNKNOWN"),
                    entry_price=ep,
                    stop_loss=entry.get("stop_loss", 0),
                    target_1=entry.get("target_1", 0),
                )

                record_push(token, "HIGH", int(prob*100))
                logger.info(
                    f"✅ 推送HIGH {direction}: {token} {prob*100:.0f}%"
                )

        # ── 推送 MEDIUM（批量）──
        medium_to_push = [
            r for r in sorted(medium_results,
                               key=lambda x: x["probability"], reverse=True)
            if (not is_muted(r["token"])
                and r["direction"] != "NEUTRAL"
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

        elapsed = time.time() - scan_start
        logger.info(
            f"扫描完成：{elapsed:.1f}秒 "
            f"HIGH={len(high_results)} MEDIUM={len(medium_results)}"
        )

        # 无异动时静默（不推送扫描总结）


def _get_alert_context(token: str) -> tuple:
    """返回（首次预警距今分钟数，价格变化率）"""
    from cache.snapshot import get_alert_history
    history = get_alert_history(token, hours=24)
    if not history:
        return 0, 0.0

    oldest_ts  = min(h["ts"] for h in history)
    first_ago  = int((time.time() * 1000 - oldest_ts) / 60000)

    snaps = get_snapshots(token, "binance", limit=1)
    curr_price = snaps[0].get("price", 0) if snaps else 0

    # 找首次预警时的价格
    first_snap_idx = len(history) - 1
    first_snaps    = get_snapshots(token, "binance",
                                   limit=first_snap_idx + 2)
    first_price    = first_snaps[-1].get("price", 0) if first_snaps else 0

    price_change = (
        (curr_price - first_price) / first_price
        if first_price > 0 and curr_price > 0 else 0.0
    )

    return first_ago, price_change


# ============================================================
# CLI 入口
# ============================================================

async def main():
    ensure_shared_dir()
    init_db()
    init_backtest_tables()
    init_paper_trading_tables()

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
        logger.info("上涨/下跌双向预警系统启动")
        await send(fmt_system(
            "startup",
            f"扫描间隔 {SCAN_INTERVAL_MINUTES} 分钟\n"
            f"双向预警：做多 + 做空\n"
            f"每日 00:00 UTC 推送日报\n"
            f"Paper Trading：/paper help"
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
