# ============================================================
# main.py — 主扫描循环 v2
# 新增：现货价格采集、基差计算、新操纵模式检测
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
    SCAN_INTERVAL_MINUTES, HFREQ_INTERVAL_MINUTES,
    COLDSTART_SNAPSHOTS, FILTER, ALERT_THRESHOLD,
    DEDUP_MINUTES, NOISE_THRESHOLD,
    SCAN_RESULT_PATH, ALERT_STATE_PATH, SHARED_DIR,
)
from data.fetcher import (
    fetch_all_prices, fetch_token_full, fetch_token_realtime
)
from cache.snapshot import (
    init_db, build_exchange_snapshot, save_snapshot_batch,
    save_phase, save_alert, can_push, record_push,
    get_snapshots, is_coldstart_done, get_previous_phase,
    save_oi_baseline, build_history_summary,
)
from rules.engine import RuleEngine, aggregate
from alerts.telegram import (
    send, fmt_high_alert, fmt_medium_batch,
    fmt_scan_summary, fmt_system,
)

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
# 共享存储读写
# ============================================================

def ensure_shared_dir():
    os.makedirs(SHARED_DIR, exist_ok=True)


def write_scan_result(result_data: dict):
    ensure_shared_dir()
    with open(SCAN_RESULT_PATH, "w", encoding="utf-8") as f:
        json.dump(result_data, f, ensure_ascii=False, indent=2)


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
    muted_until = state.get("muted", {}).get(token, 0)
    return time.time() < muted_until


# ============================================================
# 第一层过滤
# ============================================================

async def layer1_filter(session: aiohttp.ClientSession) -> list:
    """
    拉取全量合约价格，筛选出候选代币
    条件：4h 价格变动 > 5% 或在高频监控列表中
    """
    logger.info("第一层过滤：拉取全市场价格...")
    all_prices = await fetch_all_prices(session)
    if not all_prices:
        logger.error("全市场价格拉取失败")
        return []

    candidates = []

    for symbol, price in all_prices.items():
        if not symbol.endswith("USDT"):
            continue
        token = symbol.replace("USDT", "")

        # 已在高频监控列表：直接加入
        if token in hfreq_tokens:
            candidates.append({"token": token, "price": price,
                                "reason": "高频监控"})
            continue

        # 与 4h 前快照对比
        snaps = get_snapshots(token, "binance", limit=17)
        if len(snaps) >= 16:
            old_price = snaps[-1].get("price", 0)
            if old_price > 0:
                change = abs((price - old_price) / old_price)
                if change > FILTER["price_change_4h"]:
                    candidates.append({
                        "token":  token,
                        "price":  price,
                        "change": change,
                        "reason": f"4h变动{change*100:.1f}%",
                    })
        else:
            # 快照不足（新代币或系统刚启动）：加入候选
            candidates.append({"token": token, "price": price,
                                "reason": "新代币/快照不足"})

    logger.info(
        f"第一层过滤：{len(all_prices)}个合约 → {len(candidates)}个候选"
    )
    return candidates


# ============================================================
# 第二层：单币完整数据采集 + 规则计算
# ============================================================

async def process_token(session: aiohttp.ClientSession,
                        token: str) -> dict | None:
    """
    采集单个代币的完整数据（合约 + 现货），
    运行规则引擎，存入快照，返回结果
    """
    try:
        raw = await fetch_token_full(session, token)
        if not raw:
            return None

        # 聚合数据（含基差计算）
        agg = aggregate(token, raw)

        # 存入快照
        exchange_data = build_exchange_snapshot(token, raw, agg)
        save_snapshot_batch(token, exchange_data)

        # 运行规则引擎
        cold_done = is_coldstart_done(token, COLDSTART_SNAPSHOTS)
        result = engine.run(
            token, agg,
            snapshot_fn=get_snapshots,
            coldstart_done=cold_done,
        )

        # 保存阶段和告警记录
        if result["score"] >= ALERT_THRESHOLD["WATCH"]:
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
# 噪音过滤
# ============================================================

def filter_noise(all_results: list) -> list:
    """
    超过 NOISE_THRESHOLD 个代币触发同一规则
    → 该规则本轮静默（市场整体行情，非单币操纵）
    """
    from collections import Counter
    rule_counts = Counter(
        r["rule"]
        for res in all_results
        for r in res.get("triggered", [])
    )
    noise_rules = {
        rule for rule, cnt in rule_counts.items()
        if cnt > NOISE_THRESHOLD
    }
    if noise_rules:
        logger.info(f"噪音规则过滤: {noise_rules}")

    filtered = []
    for res in all_results:
        clean = [r for r in res["triggered"]
                 if r["rule"] not in noise_rules]
        score = sum(r["score"] for r in clean)
        if score >= ALERT_THRESHOLD["WATCH"]:
            level = ("HIGH"   if score >= ALERT_THRESHOLD["HIGH"]
                     else "MEDIUM" if score >= ALERT_THRESHOLD["MEDIUM"]
                     else "WATCH")
            filtered.append({**res, "triggered": clean,
                             "score": score, "level": level})
    return filtered


# ============================================================
# OI 基线更新（每日一次）
# ============================================================

async def update_oi_baselines(session: aiohttp.ClientSession,
                               tokens: list):
    global _last_baseline_update
    if time.time() - _last_baseline_update < 86400:
        return
    logger.info("更新 OI 历史基线...")
    from data.fetcher import BinanceFuturesFetcher
    bf = BinanceFuturesFetcher()
    for token in tokens[:30]:
        hist = await bf.oi_history(session, token + "USDT")
        if hist and len(hist) >= 10:
            vals = [h["oi_usd"] for h in hist if h["oi_usd"] > 0]
            if vals:
                mean = statistics.mean(vals)
                std  = statistics.stdev(vals) if len(vals) > 1 else mean * 0.1
                save_oi_baseline(token, "binance", mean, std)
    _last_baseline_update = time.time()
    logger.info("OI 基线更新完成")


# ============================================================
# 单次完整扫描
# ============================================================

async def run_scan():
    scan_start = time.time()
    logger.info("=" * 55)
    logger.info("开始扫描...")

    async with aiohttp.ClientSession() as session:

        # 第一层过滤
        candidates = await layer1_filter(session)
        if not candidates:
            logger.warning("候选池为空，跳过")
            return

        # 后台更新 OI 基线
        asyncio.create_task(
            update_oi_baselines(session,
                                [c["token"] for c in candidates])
        )

        # 第二层：并发采集（每批 20 个）
        logger.info(f"第二层深度采集：{len(candidates)} 个代币...")
        all_results = []
        BATCH = 20

        for i in range(0, len(candidates), BATCH):
            batch   = candidates[i:i+BATCH]
            tasks   = [process_token(session, c["token"]) for c in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for res in results:
                if isinstance(res, dict) and res:
                    all_results.append(res)
                    if res["score"] >= ALERT_THRESHOLD["WATCH"]:
                        logger.info(
                            f"{res['token']}: 评分{res['score']} "
                            f"{res['level']} {res['phase']}"
                        )

            if i + BATCH < len(candidates):
                await asyncio.sleep(0.5)

        # 噪音过滤
        filtered = filter_noise(all_results)

        # 分级
        high_results   = [r for r in filtered if r["level"] == "HIGH"]
        medium_results = [r for r in filtered if r["level"] == "MEDIUM"]

        # 更新高频监控列表
        hfreq_tokens.clear()
        for r in filtered:
            if r["score"] >= ALERT_THRESHOLD["MEDIUM"]:
                hfreq_tokens.add(r["token"])

        # 写入共享存储（供 OpenClaw Skill 读取）
        scan_result = {
            "scan_ts":      int(time.time()),
            "scan_id":      int(time.time()),
            "duration_sec": time.time() - scan_start,
            "total_scanned":len(candidates),
            "high_alerts": [
                {
                    "token":     r["token"],
                    "score":     r["score"],
                    "level":     r["level"],
                    "phase":     r["phase"],
                    "triggered": r["triggered"][:10],
                    "pushed":    False,
                }
                for r in high_results
            ],
            "medium_alerts": [
                {
                    "token": r["token"],
                    "score": r["score"],
                    "phase": r["phase"],
                    "pushed": False,
                }
                for r in medium_results
            ],
            "system": {
                "coldstart_done": is_coldstart_done(
                    candidates[0]["token"] if candidates else "BTC",
                    COLDSTART_SNAPSHOTS
                ),
                "hfreq_tokens": list(hfreq_tokens),
                "next_scan_ts": int(time.time() + SCAN_INTERVAL_MINUTES * 60),
            }
        }
        write_scan_result(scan_result)

        # ── 推送 HIGH ALERT ──
        for r in sorted(high_results,
                         key=lambda x: x["score"], reverse=True):
            token = r["token"]
            score = r["score"]
            if is_muted(token):
                logger.info(f"{token} 已静默，跳过推送")
                continue
            if can_push(token, "HIGH", score, DEDUP_MINUTES["HIGH"]):
                # 计算首次异动时间
                first_ago = _calc_first_alert_ago(token)
                msg = fmt_high_alert(r, first_ago)
                ok  = await send(msg)
                if ok:
                    record_push(token, "HIGH", score)
                    logger.info(f"✅ 推送 HIGH: {token} 评分{score}")

        # ── 推送 MEDIUM ALERT（批量）──
        medium_to_push = [
            r for r in sorted(medium_results,
                               key=lambda x: x["score"], reverse=True)
            if (not is_muted(r["token"])
                and can_push(r["token"], "MEDIUM",
                             r["score"], DEDUP_MINUTES["MEDIUM"]))
        ]
        if medium_to_push:
            msg = fmt_medium_batch(medium_to_push)
            ok  = await send(msg)
            if ok:
                for r in medium_to_push:
                    record_push(r["token"], "MEDIUM", r["score"])
                logger.info(f"✅ 推送 MEDIUM: {len(medium_to_push)} 个代币")

        # ── 冷启动状态 ──
        cold_tokens = [
            c["token"] for c in candidates
            if not is_coldstart_done(c["token"], COLDSTART_SNAPSHOTS)
        ]

        # ── TWAP 检测激活通知（仅一次）──
        elapsed_min = (time.time() - START_TIME) / 60
        if (int(elapsed_min) == COLDSTART_SNAPSHOTS * SCAN_INTERVAL_MINUTES
                and elapsed_min < COLDSTART_SNAPSHOTS * SCAN_INTERVAL_MINUTES + 1):
            await send(fmt_system("twap_ready"))

        # ── 扫描总结 ──
        elapsed = time.time() - scan_start
        summary = fmt_scan_summary(
            high=len(high_results),
            medium=len(medium_results),
            total=len(candidates),
            elapsed=elapsed,
            next_min=SCAN_INTERVAL_MINUTES,
            coldstart_tokens=cold_tokens[:3] if cold_tokens else None,
        )
        await send(summary)

        logger.info(
            f"扫描完成：{elapsed:.1f}秒 "
            f"HIGH={len(high_results)} MEDIUM={len(medium_results)}"
        )


def _calc_first_alert_ago(token: str) -> int:
    """计算该代币首次触发异动距今多少分钟"""
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
        # cron job 模式：执行一次扫描
        await run_scan()

    elif "--update-baseline" in args:
        # OI 基线更新模式
        global _last_baseline_update
        _last_baseline_update = 0
        async with aiohttp.ClientSession() as session:
            from cache.snapshot import get_snapshots as gs
            conn = __import__("sqlite3").connect(
                __import__("config").DB_PATH
            )
            c = conn.cursor()
            c.execute("SELECT DISTINCT token FROM snapshots")
            tokens = [row[0] for row in c.fetchall()]
            conn.close()
            await update_oi_baselines(session, tokens)

    elif "--heartbeat" in args:
        # 心跳模式
        state = read_alert_state()
        hfreq = state.get("hfreq_tokens", [])
        msg = fmt_system(
            "heartbeat",
            f"高频监控: {', '.join(hfreq) if hfreq else '无'}"
        )
        await send(msg)

    else:
        # 持续运行模式（默认）
        logger.info("妖币监控系统启动（持续运行模式）")
        await send(fmt_system(
            "startup",
            f"扫描间隔 {SCAN_INTERVAL_MINUTES} 分钟\n"
            f"冷启动期 {COLDSTART_SNAPSHOTS * SCAN_INTERVAL_MINUTES} 分钟"
        ))

        scan_count = 0
        while True:
            try:
                await run_scan()
                scan_count += 1
            except Exception as e:
                logger.exception(f"扫描异常: {e}")
                await send(fmt_system("error", str(e)))
            await asyncio.sleep(SCAN_INTERVAL_MINUTES * 60)


if __name__ == "__main__":
    asyncio.run(main())
