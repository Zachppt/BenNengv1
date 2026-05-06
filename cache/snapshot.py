# ============================================================
# cache/snapshot.py — 缓存层 v2
# 新增：现货价格字段、基差字段
# ============================================================

import json
import time
import sqlite3
import logging
from typing import Optional
from config import DB_PATH, SNAPSHOT_RETENTION

logger = logging.getLogger(__name__)


# ────────────────────────────────────────
# 初始化
# ────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            token       TEXT NOT NULL,
            exchange    TEXT NOT NULL,
            ts          INTEGER NOT NULL,
            data_json   TEXT NOT NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_snap ON snapshots(token,exchange,ts)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            token       TEXT NOT NULL,
            score       INTEGER,
            level       TEXT,
            phase       TEXT,
            detail_json TEXT,
            ts          INTEGER NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS push_log (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            token   TEXT NOT NULL,
            level   TEXT NOT NULL,
            score   INTEGER NOT NULL,
            ts      INTEGER NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS system_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()
    logger.info("数据库初始化完成")


# ────────────────────────────────────────
# 快照写入
# ────────────────────────────────────────

def save_snapshot_batch(token: str, exchange_data: dict):
    """
    批量写入多个交易所的快照
    exchange_data 结构：
    {
      "binance": { price, spot_price, basis, imbalance, ... },
      "okx": { ... },
      ...
    }
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    ts = int(time.time() * 1000)

    for exchange, data in exchange_data.items():
        if not data:
            continue
        c.execute(
            "INSERT INTO snapshots (token,exchange,ts,data_json) VALUES (?,?,?,?)",
            (token, exchange, ts, json.dumps(data))
        )
        # 清理超过保留上限的旧快照
        c.execute("""
            DELETE FROM snapshots WHERE id IN (
                SELECT id FROM snapshots
                WHERE token=? AND exchange=?
                ORDER BY ts DESC
                LIMIT -1 OFFSET ?
            )
        """, (token, exchange, SNAPSHOT_RETENTION))

    conn.commit()
    conn.close()


# ────────────────────────────────────────
# 快照读取
# ────────────────────────────────────────

def get_snapshots(token: str, exchange: str, limit: int = 10) -> list:
    """获取最近 N 条快照（时间倒序，最新在前）"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT ts, data_json FROM snapshots
        WHERE token=? AND exchange=?
        ORDER BY ts DESC LIMIT ?
    """, (token, exchange, limit))
    rows = c.fetchall()
    conn.close()
    return [{"ts": row[0], **json.loads(row[1])} for row in rows]


def get_latest_snapshot(token: str, exchange: str) -> Optional[dict]:
    snaps = get_snapshots(token, exchange, limit=1)
    return snaps[0] if snaps else None


def get_snapshot_count(token: str, exchange: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT COUNT(*) FROM snapshots WHERE token=? AND exchange=?",
        (token, exchange)
    )
    count = c.fetchone()[0]
    conn.close()
    return count


def is_coldstart_done(token: str, min_snapshots: int = 16) -> bool:
    """是否度过冷启动期（默认 16 条快照 = 4 小时）"""
    return get_snapshot_count(token, "binance") >= min_snapshots


# ────────────────────────────────────────
# 快照构建（从 raw 数据提取关键字段）
# ────────────────────────────────────────

def build_exchange_snapshot(token: str, raw: dict, agg: dict) -> dict:
    """
    从 fetch_token_full() 的 raw 数据和聚合结果
    提取需要存入快照的关键字段
    """
    exchange_data = {}

    for ex in ["binance", "okx", "bybit", "bitget"]:
        ft = raw.get(ex, {}).get("futures", {})
        sp = raw.get(ex, {}).get("spot", {})

        bt  = ft.get("book_ticker") or {}
        dep = ft.get("depth") or {}
        oi  = ft.get("oi") or {}
        fr  = ft.get("funding") or {}

        # 现货价格
        spot_raw  = sp.get("price")
        spot_price = 0
        if isinstance(spot_raw, dict):
            spot_price = spot_raw.get("price", 0)
        elif isinstance(spot_raw, (int, float)):
            spot_price = spot_raw

        # 基差
        basis_pct = agg.get("basis", {}).get(ex, 0)

        exchange_data[ex] = {
            # 合约价格
            "price":         bt.get("mid", 0),
            "bid":           bt.get("bid", 0),
            "ask":           bt.get("ask", 0),
            "spread":        bt.get("spread", 0),
            # 现货价格（新增）
            "spot_price":    spot_price,
            # 基差（新增）
            "basis":         basis_pct,
            # 订单簿
            "imbalance":     bt.get("imbalance", 0),
            "bid_depth_usd": dep.get("bid_depth_usd", 0),
            "ask_depth_usd": dep.get("ask_depth_usd", 0),
            "depth_ratio":   dep.get("depth_ratio", 0),
            "large_bids":    dep.get("large_bids", []),
            # OI
            "oi":            oi.get("oi", 0),
            "oi_usd":        oi.get("oi_usd", 0),
            # 资金费率
            "funding":       fr.get("current", 0),
            # 聚合字段（全平台汇总）
            "max_spread":    agg.get("max_futures_spread", 0),
            "max_basis":     agg.get("max_basis", 0),
            "total_oi":      agg.get("total_oi", 0),
            "spot_avg":      agg.get("spot_avg", 0),
        }

    return exchange_data


# ────────────────────────────────────────
# 历史摘要（供 LLM 使用）
# ────────────────────────────────────────

def build_history_summary(token: str) -> dict:
    """从快照提取关键历史趋势，供 token-analyzer LLM Prompt 使用"""
    summary = {}

    for ex in ["binance", "bybit"]:
        snaps = get_snapshots(token, ex, limit=8)
        if not snaps:
            continue

        # 时间正序
        snaps_asc = list(reversed(snaps))

        # 失衡度趋势
        imbs = [s.get("imbalance", 0) for s in snaps_asc]
        if any(v != 0 for v in imbs):
            summary[f"{ex}_失衡度趋势"] = " → ".join(f"{v:.2f}" for v in imbs)

        # OI 趋势（百万元）
        ois = [s.get("oi_usd", 0) / 1e6 for s in snaps_asc]
        if any(v > 0 for v in ois):
            summary[f"{ex}_OI趋势(M)"] = " → ".join(f"{v:.1f}" for v in ois)

        # 资金费率趋势
        frs = [s.get("funding", 0) * 100 for s in snaps_asc]
        if any(v != 0 for v in frs):
            summary[f"{ex}_资金费率趋势(%)"] = " → ".join(f"{v:.4f}" for v in frs)

        # 现货价格趋势（新增）
        sps = [s.get("spot_price", 0) for s in snaps_asc if s.get("spot_price")]
        if sps:
            summary[f"{ex}_现货价格趋势"] = " → ".join(f"${v:.5g}" for v in sps)

        # 基差趋势（新增）
        bs = [s.get("basis", 0) * 100 for s in snaps_asc]
        if any(abs(v) > 0.1 for v in bs):
            summary[f"{ex}_基差趋势(%)"] = " → ".join(f"{v:.3f}" for v in bs)

        # 仅 binance：跨所价差趋势
        if ex == "binance":
            spreads = [s.get("max_spread", 0) * 100 for s in snaps_asc]
            if any(v > 0.1 for v in spreads):
                summary["跨所价差趋势(%)"] = " → ".join(f"{v:.3f}" for v in spreads)

    return summary


# ────────────────────────────────────────
# 历史异动记录（/history 指令用）
# ────────────────────────────────────────

def get_alert_history(token: str, hours: int = 24) -> list:
    """获取该代币过去 N 小时的异动评分记录"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cutoff = int((time.time() - hours * 3600) * 1000)
    c.execute("""
        SELECT ts, score, level, phase FROM alerts
        WHERE token=? AND ts > ?
        ORDER BY ts DESC
    """, (token, cutoff))
    rows = c.fetchall()
    conn.close()
    return [
        {
            "ts":    row[0],
            "score": row[1],
            "level": row[2],
            "phase": row[3],
            "time_str": time.strftime(
                "%H:%M UTC", time.gmtime(row[0] / 1000)
            ),
        }
        for row in rows
    ]


def save_alert(token: str, score: int, level: str,
               phase: str, triggered: list):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO alerts (token,score,level,phase,detail_json,ts)
        VALUES (?,?,?,?,?,?)
    """, (
        token, score, level, phase,
        json.dumps(triggered[:5]),
        int(time.time() * 1000)
    ))
    conn.commit()
    conn.close()


# ────────────────────────────────────────
# 前序阶段（解决历史维度问题）
# ────────────────────────────────────────

def save_phase(token: str, phase: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO system_state (key, value)
        VALUES (?, ?)
    """, (f"phase_{token}",
          json.dumps({"phase": phase, "ts": int(time.time())})))
    conn.commit()
    conn.close()


def get_previous_phase(token: str) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM system_state WHERE key=?",
              (f"phase_{token}",))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    data = json.loads(row[0])
    # 1 小时内的前序阶段有效
    if time.time() - data["ts"] < 3600:
        return data["phase"]
    return None


# ────────────────────────────────────────
# OI 历史基线
# ────────────────────────────────────────

def save_oi_baseline(token: str, exchange: str, mean: float, std: float):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO system_state (key, value)
        VALUES (?, ?)
    """, (f"oi_baseline_{token}_{exchange}",
          json.dumps({"mean": mean, "std": std, "ts": int(time.time())})))
    conn.commit()
    conn.close()


def get_oi_baseline(token: str, exchange: str) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM system_state WHERE key=?",
              (f"oi_baseline_{token}_{exchange}",))
    row = c.fetchone()
    conn.close()
    return json.loads(row[0]) if row else None


# ────────────────────────────────────────
# 推送去重
# ────────────────────────────────────────

def can_push(token: str, level: str, score: int,
             dedup_minutes: int = 30) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    cutoff = int((time.time() - dedup_minutes * 60) * 1000)
    c.execute("""
        SELECT score FROM push_log
        WHERE token=? AND level=? AND ts>?
        ORDER BY ts DESC LIMIT 1
    """, (token, level, cutoff))
    row = c.fetchone()
    conn.close()

    if not row:
        return True
    # 评分明显上升则强制推送
    return score >= row[0] + 2


def record_push(token: str, level: str, score: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO push_log (token,level,score,ts) VALUES (?,?,?,?)",
        (token, level, score, int(time.time() * 1000))
    )
    # 清理 7 天前记录
    c.execute("DELETE FROM push_log WHERE ts < ?",
              (int((time.time() - 7 * 86400) * 1000),))
    conn.commit()
    conn.close()


# ────────────────────────────────────────
# 清理过期快照（cron job 调用）
# ────────────────────────────────────────

def clean_old_snapshots():
    """
    清理策略：
    - 活跃代币（7 天内有告警）：保留 96 条
    - 非活跃（7~30 天）：保留 24 条
    - 超 30 天无告警：删除快照
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    now = int(time.time() * 1000)
    d7  = now - 7  * 86400 * 1000
    d30 = now - 30 * 86400 * 1000

    # 获取活跃代币
    c.execute("SELECT DISTINCT token FROM alerts WHERE ts > ?", (d7,))
    active_tokens = {row[0] for row in c.fetchall()}

    # 获取所有代币
    c.execute("SELECT DISTINCT token FROM snapshots")
    all_tokens = {row[0] for row in c.fetchall()}

    for token in all_tokens:
        if token in active_tokens:
            # 活跃：保留 96 条
            retain = 96
        else:
            # 检查是否超过 30 天
            c.execute(
                "SELECT MAX(ts) FROM alerts WHERE token=?", (token,)
            )
            last = c.fetchone()[0]
            if not last or last < d30:
                # 超过 30 天：删除所有快照
                c.execute(
                    "DELETE FROM snapshots WHERE token=?", (token,)
                )
                logger.info(f"清理非活跃代币快照: {token}")
                continue
            else:
                # 7~30 天：保留 24 条
                retain = 24

        for ex in ["binance", "okx", "bybit", "bitget"]:
            c.execute("""
                DELETE FROM snapshots WHERE id IN (
                    SELECT id FROM snapshots
                    WHERE token=? AND exchange=?
                    ORDER BY ts DESC
                    LIMIT -1 OFFSET ?
                )
            """, (token, ex, retain))

    conn.commit()
    conn.close()
    logger.info("快照清理完成")


# ────────────────────────────────────────
# CLI 入口（cron job 调用）
# ────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if "--clean" in sys.argv:
        init_db()
        clean_old_snapshots()
        print("快照清理完成")
