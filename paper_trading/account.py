# ============================================================
# paper_trading/account.py — Paper Trading系统 v4
# 基于Telegram用户ID的模拟交易账户
# ============================================================

import sqlite3
import json
import time
import logging
from config import DB_PATH

logger = logging.getLogger(__name__)

INITIAL_BALANCE = 10000.0   # 初始资金 $10,000 USDT
MAX_POSITION_PCT = 0.20     # 单笔最大仓位20%


# ============================================================
# 初始化
# ============================================================

def init_paper_trading_tables():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # 账户表
    c.execute("""
        CREATE TABLE IF NOT EXISTS paper_accounts (
            user_id       TEXT PRIMARY KEY,
            username      TEXT,
            balance_usdt  REAL DEFAULT 10000.0,
            total_pnl     REAL DEFAULT 0.0,
            win_count     INTEGER DEFAULT 0,
            loss_count    INTEGER DEFAULT 0,
            created_ts    INTEGER,
            updated_ts    INTEGER
        )
    """)

    # 持仓表
    c.execute("""
        CREATE TABLE IF NOT EXISTS paper_positions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       TEXT NOT NULL,
            token         TEXT NOT NULL,
            direction     TEXT NOT NULL,   -- LONG / SHORT
            size_usdt     REAL NOT NULL,
            entry_price   REAL NOT NULL,
            stop_loss     REAL,
            target_1      REAL,
            signal_prob   REAL,
            phase         TEXT,
            opened_ts     INTEGER NOT NULL,
            status        TEXT DEFAULT 'OPEN',  -- OPEN / CLOSED
            close_price   REAL,
            close_ts      INTEGER,
            pnl           REAL,
            close_reason  TEXT   -- TP / SL / MANUAL
        )
    """)

    conn.commit()
    conn.close()


# ============================================================
# 账户管理
# ============================================================

def get_or_create_account(user_id: str,
                           username: str = "") -> dict:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT * FROM paper_accounts WHERE user_id=?", (user_id,))
    row = c.fetchone()

    if not row:
        now = int(time.time() * 1000)
        c.execute("""
            INSERT INTO paper_accounts
            (user_id, username, balance_usdt, total_pnl,
             win_count, loss_count, created_ts, updated_ts)
            VALUES (?,?,?,?,?,?,?,?)
        """, (user_id, username, INITIAL_BALANCE, 0, 0, 0, now, now))
        conn.commit()
        account = {
            "user_id": user_id, "username": username,
            "balance_usdt": INITIAL_BALANCE, "total_pnl": 0,
            "win_count": 0, "loss_count": 0,
        }
    else:
        cols = ["user_id","username","balance_usdt","total_pnl",
                "win_count","loss_count","created_ts","updated_ts"]
        account = dict(zip(cols, row))

    conn.close()
    return account


# ============================================================
# 开仓
# ============================================================

def open_position(user_id: str, token: str, direction: str,
                   size_usdt: float, entry_price: float,
                   stop_loss: float = None, target_1: float = None,
                   signal_prob: float = 0, phase: str = "",
                   username: str = "") -> dict:
    """
    开仓
    返回：{"ok": True/False, "message": "...", "position": {...}}
    """
    account = get_or_create_account(user_id, username)

    # 检查余额
    if size_usdt > account["balance_usdt"]:
        return {"ok": False,
                "message": f"余额不足（可用 ${account['balance_usdt']:,.2f}）"}

    # 检查单笔仓位限制
    max_size = INITIAL_BALANCE * MAX_POSITION_PCT
    if size_usdt > max_size:
        return {"ok": False,
                "message": (f"单笔仓位不能超过 ${max_size:,.0f} "
                           f"（总资金的{MAX_POSITION_PCT*100:.0f}%）")}

    # 检查是否已有该代币持仓
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT id FROM paper_positions
        WHERE user_id=? AND token=? AND status='OPEN'
    """, (user_id, token))
    if c.fetchone():
        conn.close()
        return {"ok": False,
                "message": f"已有 {token} 持仓，请先平仓"}

    # 扣除余额
    now = int(time.time() * 1000)
    c.execute("""
        UPDATE paper_accounts
        SET balance_usdt = balance_usdt - ?,
            updated_ts = ?
        WHERE user_id=?
    """, (size_usdt, now, user_id))

    # 创建持仓
    c.execute("""
        INSERT INTO paper_positions
        (user_id, token, direction, size_usdt, entry_price,
         stop_loss, target_1, signal_prob, phase, opened_ts)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (user_id, token, direction, size_usdt, entry_price,
          stop_loss, target_1, signal_prob, phase, now))

    pos_id = c.lastrowid
    conn.commit()
    conn.close()

    direction_str = "多" if direction == "LONG" else "空"
    msg = (
        f"✅ {token} 开{direction_str}仓成功\n"
        f"入场价：${_fmt(entry_price)}\n"
        f"仓位：${size_usdt:,.0f} USDT\n"
    )
    if stop_loss:
        msg += f"止损：${_fmt(stop_loss)}\n"
    if target_1:
        msg += f"目标：${_fmt(target_1)}\n"

    return {
        "ok":      True,
        "message": msg,
        "position": {
            "id": pos_id, "token": token,
            "direction": direction, "size_usdt": size_usdt,
            "entry_price": entry_price,
        }
    }


# ============================================================
# 平仓
# ============================================================

def close_position(user_id: str, token: str,
                    current_price: float,
                    reason: str = "MANUAL") -> dict:
    """平仓并计算盈亏"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        SELECT id, direction, size_usdt, entry_price
        FROM paper_positions
        WHERE user_id=? AND token=? AND status='OPEN'
        ORDER BY opened_ts DESC LIMIT 1
    """, (user_id, token))

    row = c.fetchone()
    if not row:
        conn.close()
        return {"ok": False, "message": f"未找到 {token} 的持仓"}

    pos_id, direction, size_usdt, entry_price = row

    # 计算盈亏
    if direction == "LONG":
        pnl_pct = (current_price - entry_price) / entry_price
    else:  # SHORT
        pnl_pct = (entry_price - current_price) / entry_price

    pnl_usdt = size_usdt * pnl_pct
    now      = int(time.time() * 1000)

    # 更新持仓
    c.execute("""
        UPDATE paper_positions
        SET status='CLOSED', close_price=?, close_ts=?,
            pnl=?, close_reason=?
        WHERE id=?
    """, (current_price, now, pnl_usdt, reason, pos_id))

    # 返还资金+盈亏
    returned = size_usdt + pnl_usdt
    c.execute("""
        UPDATE paper_accounts
        SET balance_usdt = balance_usdt + ?,
            total_pnl = total_pnl + ?,
            win_count = win_count + ?,
            loss_count = loss_count + ?,
            updated_ts = ?
        WHERE user_id=?
    """, (
        returned, pnl_usdt,
        1 if pnl_usdt > 0 else 0,
        1 if pnl_usdt < 0 else 0,
        now, user_id
    ))

    conn.commit()
    conn.close()

    direction_str = "多" if direction == "LONG" else "空"
    pnl_icon = "✅" if pnl_usdt > 0 else "❌"

    msg = (
        f"{pnl_icon} {token} 平{direction_str}仓\n"
        f"入场：${_fmt(entry_price)} → 平仓：${_fmt(current_price)}\n"
        f"盈亏：{'+' if pnl_usdt >= 0 else ''}"
        f"${pnl_usdt:,.2f} ({pnl_pct*100:+.2f}%)\n"
    )

    return {"ok": True, "message": msg, "pnl": pnl_usdt}


# ============================================================
# 账户状态查询
# ============================================================

def get_account_status(user_id: str,
                        get_prices_fn=None) -> str:
    """返回账户状态的Telegram格式文字"""
    account = get_or_create_account(user_id)
    conn    = sqlite3.connect(DB_PATH)
    c       = conn.cursor()

    # 当前持仓
    c.execute("""
        SELECT token, direction, size_usdt, entry_price, opened_ts
        FROM paper_positions
        WHERE user_id=? AND status='OPEN'
        ORDER BY opened_ts DESC
    """, (user_id,))
    positions = c.fetchall()

    # 历史统计
    c.execute("""
        SELECT COUNT(*), SUM(pnl),
               AVG(CASE WHEN pnl > 0 THEN pnl END),
               AVG(CASE WHEN pnl < 0 THEN pnl END)
        FROM paper_positions
        WHERE user_id=? AND status='CLOSED'
    """, (user_id,))
    hist = c.fetchone()
    conn.close()

    balance   = account["balance_usdt"]
    total_pnl = account["total_pnl"]
    wins      = account["win_count"]
    losses    = account["loss_count"]
    total_trades = wins + losses
    win_rate  = wins / total_trades if total_trades > 0 else 0

    pnl_icon = "📈" if total_pnl >= 0 else "📉"

    lines = [
        "💼 <b>Paper Trading 账户</b>",
        "",
        f"可用余额：${balance:,.2f} USDT",
        f"总盈亏：{pnl_icon} {'+' if total_pnl >= 0 else ''}${total_pnl:,.2f}",
        f"胜率：{win_rate*100:.1f}% （{wins}胜/{losses}负）",
        "",
    ]

    if positions:
        lines.append(f"📊 <b>当前持仓（{len(positions)}个）</b>")
        for token, direction, size, entry, ts in positions:
            d_str = "多" if direction == "LONG" else "空"
            lines.append(
                f"  {token} {d_str}仓 ${size:,.0f} "
                f"@ ${_fmt(entry)}"
            )
        lines.append("")

    lines += [
        "指令：",
        "/paper long TOKEN 金额  开多仓",
        "/paper short TOKEN 金额 开空仓",
        "/paper close TOKEN      平仓",
        "/paper history          历史记录",
        "/paper leaderboard      排行榜",
    ]

    return "\n".join(lines)


# ============================================================
# 历史记录
# ============================================================

def get_trade_history(user_id: str, limit: int = 10) -> str:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        SELECT token, direction, size_usdt, entry_price,
               close_price, pnl, close_ts, close_reason
        FROM paper_positions
        WHERE user_id=? AND status='CLOSED'
        ORDER BY close_ts DESC
        LIMIT ?
    """, (user_id, limit))

    rows = c.fetchall()
    conn.close()

    if not rows:
        return "暂无交易记录"

    lines = [f"📋 <b>交易记录（最近{limit}笔）</b>", ""]

    for (token, direction, size, entry, close,
         pnl, close_ts, reason) in rows:
        d_str    = "多" if direction == "LONG" else "空"
        pnl_icon = "✅" if pnl > 0 else "❌"
        ts_str   = time.strftime(
            "%m-%d %H:%M", time.gmtime(close_ts / 1000)
        ) if close_ts else "—"
        lines.append(
            f"{pnl_icon} {token} {d_str}  "
            f"{'+' if pnl >= 0 else ''}${pnl:,.2f}  "
            f"{ts_str}"
        )

    return "\n".join(lines)


# ============================================================
# 排行榜
# ============================================================

def get_leaderboard(limit: int = 10) -> str:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        SELECT username, balance_usdt, total_pnl,
               win_count, loss_count
        FROM paper_accounts
        ORDER BY total_pnl DESC
        LIMIT ?
    """, (limit,))

    rows = c.fetchall()
    conn.close()

    if not rows:
        return "暂无排行榜数据"

    lines = ["🏆 <b>Paper Trading 排行榜</b>", ""]
    medals = ["🥇", "🥈", "🥉"]

    for i, (username, balance, pnl, wins, losses) in enumerate(rows):
        total  = wins + losses
        wr     = wins / total if total > 0 else 0
        medal  = medals[i] if i < 3 else f"#{i+1}"
        name   = username or f"User_{i+1}"
        pnl_str= f"{'+' if pnl >= 0 else ''}${pnl:,.2f}"

        lines.append(
            f"{medal} <b>{name}</b>  {pnl_str}  胜率{wr*100:.0f}%"
        )

    return "\n".join(lines)


# ============================================================
# 指令解析（供 agent/analyzer.py 调用）
# ============================================================

async def handle_paper_command(text: str, user_id: str,
                                username: str,
                                get_price_fn=None) -> str:
    """
    解析 /paper 指令并执行
    """
    parts = text.strip().split()
    if len(parts) < 2:
        return _paper_help()

    sub = parts[1].lower()

    if sub == "status":
        return get_account_status(user_id)

    elif sub == "history":
        return get_trade_history(user_id)

    elif sub == "leaderboard":
        return get_leaderboard()

    elif sub in ("long", "short"):
        if len(parts) < 4:
            return "格式：/paper long TOKEN 金额\n例：/paper long TRIA 500"

        token     = parts[2].upper().replace("USDT", "")
        try:
            size_usdt = float(parts[3])
        except ValueError:
            return "金额格式错误，例：/paper long TRIA 500"

        # 获取当前价格
        if not get_price_fn:
            return "价格服务暂不可用，请稍后重试"

        price = await get_price_fn(token + "USDT")
        if not price:
            return f"无法获取 {token} 价格，请确认代币名称"

        direction = "LONG" if sub == "long" else "SHORT"
        result    = open_position(
            user_id=user_id,
            token=token,
            direction=direction,
            size_usdt=size_usdt,
            entry_price=price,
            username=username,
        )
        return result["message"]

    elif sub == "close":
        if len(parts) < 3:
            return "格式：/paper close TOKEN\n例：/paper close TRIA"

        token = parts[2].upper().replace("USDT", "")

        if not get_price_fn:
            return "价格服务暂不可用"

        price = await get_price_fn(token + "USDT")
        if not price:
            return f"无法获取 {token} 价格"

        result = close_position(user_id, token, price)
        return result["message"]

    else:
        return _paper_help()


def _paper_help() -> str:
    return (
        "📖 <b>Paper Trading 指令</b>\n\n"
        "/paper long TOKEN 金额   开多仓\n"
        "/paper short TOKEN 金额  开空仓\n"
        "/paper close TOKEN       平仓\n"
        "/paper status            账户状态\n"
        "/paper history           历史记录\n"
        "/paper leaderboard       排行榜\n\n"
        "例：/paper long TRIA 500\n"
        "    /paper close TRIA"
    )


def _fmt(p: float) -> str:
    if   p >= 1000:  return f"{p:,.2f}"
    elif p >= 1:     return f"{p:.4f}"
    elif p >= 0.001: return f"{p:.6f}"
    else:            return f"{p:.8f}"
