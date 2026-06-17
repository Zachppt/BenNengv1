# ============================================================
# backtest/recorder.py — 自动记录与评估 v4
# 每次预警自动创建记录，4h/8h/24h后评估命中率
# ============================================================

import sqlite3
import json
import time
import logging
from config import DB_PATH

logger = logging.getLogger(__name__)


# ============================================================
# 初始化
# ============================================================

def init_backtest_tables():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 预警记录表
    c.execute("""
        CREATE TABLE IF NOT EXISTS alert_records (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            token         TEXT NOT NULL,
            direction     TEXT NOT NULL,   -- LONG / SHORT
            probability   REAL NOT NULL,
            phase         TEXT,
            behavior_type TEXT,
            entry_price   REAL,
            stop_loss     REAL,
            target_1      REAL,
            alert_ts      INTEGER NOT NULL,

            -- 评估结果（4h后填入）
            price_4h      REAL,
            price_8h      REAL,
            price_24h     REAL,
            max_gain_4h   REAL,
            max_loss_4h   REAL,
            outcome_4h    TEXT,   -- WIN / LOSS / NEUTRAL / PENDING
            outcome_8h    TEXT,
            outcome_24h   TEXT,
            evaluated     INTEGER DEFAULT 0
        )
    """)

    # 每日统计缓存表
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_stats (
            date          TEXT PRIMARY KEY,
            total_alerts  INTEGER DEFAULT 0,
            long_alerts   INTEGER DEFAULT 0,
            short_alerts  INTEGER DEFAULT 0,
            long_wins     INTEGER DEFAULT 0,
            short_wins    INTEGER DEFAULT 0,
            long_total    INTEGER DEFAULT 0,
            short_total   INTEGER DEFAULT 0,
            top_tokens    TEXT
        )
    """)

    conn.commit()
    conn.close()


# ============================================================
# 记录预警
# ============================================================

def record_alert(token: str, direction: str, probability: float,
                 phase: str, behavior_type: str,
                 entry_price: float, stop_loss: float,
                 target_1: float):
    """每次推送预警时调用，自动创建记录"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO alert_records
        (token, direction, probability, phase, behavior_type,
         entry_price, stop_loss, target_1, alert_ts,
         outcome_4h, outcome_8h, outcome_24h)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        token, direction, probability, phase, behavior_type,
        entry_price, stop_loss, target_1,
        int(time.time() * 1000),
        "PENDING", "PENDING", "PENDING"
    ))
    conn.commit()
    conn.close()
    logger.debug(f"记录预警: {token} {direction} {probability:.0%}")


# ============================================================
# 评估预警结果（定时调用）
# ============================================================

async def evaluate_pending_alerts(session, fetch_price_fn):
    """
    评估所有 PENDING 的预警记录
    4h/8h/24h 后分别评估
    """
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    now = int(time.time() * 1000)
    hour_ms = 3600 * 1000

    # 查询需要评估的记录
    c.execute("""
        SELECT id, token, direction, entry_price, stop_loss, target_1,
               alert_ts, outcome_4h, outcome_8h, outcome_24h
        FROM alert_records
        WHERE evaluated = 0 AND alert_ts < ?
        ORDER BY alert_ts DESC
        LIMIT 50
    """, (now - hour_ms * 3,))   # 至少3小时后才开始评估

    rows = c.fetchall()
    conn.close()

    for row in rows:
        (rid, token, direction, entry_price,
         stop_loss, target_1, alert_ts,
         out_4h, out_8h, out_24h) = row

        elapsed_h = (now - alert_ts) / hour_ms

        try:
            current_price = await fetch_price_fn(session, token + "USDT")
            if not current_price or not entry_price:
                continue

            change = (current_price - entry_price) / entry_price

            # 根据方向判断胜负
            if direction == "LONG":
                win_threshold  =  0.05   # 涨5%算赢
                loss_threshold = -0.03   # 跌3%算输

                outcome = _judge_outcome(change, win_threshold, loss_threshold)
                max_gain = max(change, 0)
                max_loss = min(change, 0)

            else:  # SHORT
                win_threshold  = -0.05   # 跌5%算赢
                loss_threshold =  0.03   # 涨3%算输

                outcome = _judge_outcome(-change, abs(win_threshold),
                                         abs(loss_threshold))
                max_gain = max(-change, 0)
                max_loss = min(-change, 0)

            # 更新对应时间段的结果
            conn = sqlite3.connect(DB_PATH)
            c2 = conn.cursor()

            updates = {}
            if elapsed_h >= 4  and out_4h  == "PENDING":
                updates["outcome_4h"]  = outcome
                updates["price_4h"]    = current_price
                updates["max_gain_4h"] = max_gain
                updates["max_loss_4h"] = max_loss

            if elapsed_h >= 8  and out_8h  == "PENDING":
                updates["outcome_8h"]  = outcome
                updates["price_8h"]    = current_price

            if elapsed_h >= 24 and out_24h == "PENDING":
                updates["outcome_24h"] = outcome
                updates["price_24h"]   = current_price
                updates["evaluated"]   = 1

            if updates:
                set_clause = ", ".join(f"{k}=?" for k in updates)
                vals = list(updates.values()) + [rid]
                c2.execute(
                    f"UPDATE alert_records SET {set_clause} WHERE id=?",
                    vals
                )
                conn.commit()
                logger.debug(
                    f"评估 {token} {direction}: {outcome} "
                    f"({change*100:+.1f}%)"
                )

            conn.close()

        except Exception as e:
            logger.warning(f"评估失败 {token}: {e}")


def _judge_outcome(gain: float, win_thr: float,
                    loss_thr: float) -> str:
    if   gain >=  win_thr:  return "WIN"
    elif gain <= -loss_thr: return "LOSS"
    else:                   return "NEUTRAL"


# ============================================================
# 每日统计
# ============================================================

def calc_daily_stats(date_str: str = None) -> dict:
    """
    计算指定日期（默认昨天）的预警统计
    """
    if not date_str:
        # 昨天的时间范围
        now     = time.time()
        day_start = int((now - 86400) // 86400 * 86400 * 1000)
        day_end   = int(day_start + 86400 * 1000)
    else:
        import datetime
        d         = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        day_start = int(d.timestamp() * 1000)
        day_end   = day_start + 86400 * 1000

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 总预警数
    c.execute("""
        SELECT direction, COUNT(*),
               SUM(CASE WHEN outcome_4h = 'WIN' THEN 1 ELSE 0 END)
        FROM alert_records
        WHERE alert_ts >= ? AND alert_ts < ?
          AND outcome_4h != 'PENDING'
        GROUP BY direction
    """, (day_start, day_end))

    rows  = c.fetchall()
    stats = {
        "long_alerts": 0, "short_alerts": 0,
        "long_wins":   0, "short_wins":   0,
        "long_total":  0, "short_total":  0,
    }

    for direction, count, wins in rows:
        if direction == "LONG":
            stats["long_alerts"] = count
            stats["long_total"]  = count
            stats["long_wins"]   = wins or 0
        elif direction == "SHORT":
            stats["short_alerts"] = count
            stats["short_total"]  = count
            stats["short_wins"]   = wins or 0

    stats["total_alerts"] = stats["long_alerts"] + stats["short_alerts"]

    # 命中率
    if stats["long_total"] > 0:
        stats["long_win_rate"] = stats["long_wins"] / stats["long_total"]
    if stats["short_total"] > 0:
        stats["short_win_rate"] = stats["short_wins"] / stats["short_total"]

    # 最活跃代币
    c.execute("""
        SELECT token, COUNT(*) as cnt
        FROM alert_records
        WHERE alert_ts >= ? AND alert_ts < ?
        GROUP BY token
        ORDER BY cnt DESC
        LIMIT 3
    """, (day_start, day_end))

    stats["top_tokens"] = [
        {"token": row[0], "count": row[1]}
        for row in c.fetchall()
    ]

    conn.close()
    return stats


# ============================================================
# 查询历史命中率（供 /analyze 使用）
# ============================================================

def get_token_accuracy(token: str, days: int = 7) -> dict:
    """获取该代币过去N天的预警准确率"""
    cutoff = int((time.time() - days * 86400) * 1000)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        SELECT direction,
               COUNT(*) as total,
               SUM(CASE WHEN outcome_4h = 'WIN' THEN 1 ELSE 0 END) as wins,
               AVG(CASE WHEN outcome_4h = 'WIN'
                        THEN max_gain_4h ELSE max_loss_4h END) as avg_pnl
        FROM alert_records
        WHERE token=? AND alert_ts >= ? AND outcome_4h != 'PENDING'
        GROUP BY direction
    """, (token, cutoff))

    rows   = c.fetchall()
    result = {}

    for direction, total, wins, avg_pnl in rows:
        result[direction] = {
            "total":    total,
            "wins":     wins or 0,
            "win_rate": (wins or 0) / total if total > 0 else 0,
            "avg_pnl":  avg_pnl or 0,
        }

    conn.close()
    return result
