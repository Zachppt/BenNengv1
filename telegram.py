# ============================================================
# alerts/telegram.py — v4
# 双向预警格式（做多/做空）
# 每日汇总，静默无异动扫描
# ============================================================

import aiohttp
import logging
import time
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from rules.kline_analyzer import format_kline_summary
from rules.behavior_classifier import format_behavior_tag

logger = logging.getLogger(__name__)
API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
EXCHANGES = ["binance", "okx", "bybit", "bitget"]


# ────────────────────────────────────────
# 发送文字
# ────────────────────────────────────────

async def send(text: str, parse_mode: str = "HTML") -> bool:
    payload = {
        "chat_id":                  TELEGRAM_CHAT_ID,
        "text":                     text[:4096],
        "parse_mode":               parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{API}/sendMessage", json=payload,
                              timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    body = await r.text()
                    logger.error(f"推送失败: {r.status} {body[:200]}")
                    return False
                return True
    except Exception as e:
        logger.error(f"推送异常: {e}")
        return False


# ────────────────────────────────────────
# 发送图片（K线图）
# ────────────────────────────────────────

async def send_photo(photo_bytes: bytes, caption: str = "") -> bool:
    if not photo_bytes:
        return False
    try:
        data = aiohttp.FormData()
        data.add_field("chat_id", str(TELEGRAM_CHAT_ID))
        data.add_field("photo", photo_bytes,
                       filename="chart.png", content_type="image/png")
        if caption:
            data.add_field("caption", caption[:1024])
            data.add_field("parse_mode", "HTML")
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{API}/sendPhoto", data=data,
                              timeout=aiohttp.ClientTimeout(total=30)) as r:
                return r.status == 200
    except Exception as e:
        logger.error(f"图片推送异常: {e}")
        return False


# ────────────────────────────────────────
# HIGH ALERT — 做多预警
# ────────────────────────────────────────

def fmt_long_alert(result: dict, first_alert_ago_min: int = 0,
                   price_change_since_alert: float = 0) -> str:
    token  = result["token"]
    prob   = result["long_probability"]
    phase  = result["phase"]
    agg    = result["agg"]
    lt     = result["long_triggered"]
    entry  = result.get("entry_zone", {})
    clf    = result.get("classification", {})

    # 入场建议
    advice = _entry_advice(first_alert_ago_min, price_change_since_alert, phase)

    prob_icon = _prob_icon(prob)
    lines = [
        f"📡 <b>{token}/USDT</b>  做多概率 <b>{prob*100:.0f}%</b>",
        f"{prob_icon} {phase}",
        f"{format_behavior_tag(clf)}",
    ]

    if first_alert_ago_min > 0:
        lines.append(
            f"⏱ 首次预警：{first_alert_ago_min}分钟前  "
            f"已{'涨' if price_change_since_alert >= 0 else '跌'}"
            f" {abs(price_change_since_alert)*100:.1f}%"
        )
    lines.append("")

    # 入场建议（核心新增）
    lines.append(f"💡 <b>入场建议</b>：{advice}")
    lines.append("")

    # 前置信号
    from config import SIGNAL_WEIGHTS
    pre  = [(r, SIGNAL_WEIGHTS.get(r["rule"], 0))
            for r in lt if SIGNAL_WEIGHTS.get(r["rule"], 0) >= 7]
    conf = [r for r in lt if SIGNAL_WEIGHTS.get(r["rule"], 0) < 7
            and SIGNAL_WEIGHTS.get(r["rule"], 0) > 0]
    pre.sort(key=lambda x: x[1], reverse=True)

    if pre:
        lines.append("🎯 <b>前置信号</b>")
        for r, w in pre[:5]:
            lines.append(f"✅ {r['detail']}  <i>+{w}%</i>")
        lines.append("")

    # K线结构
    ka = agg.get("kline_analysis", {}).get("binance", {})
    if ka:
        kline_txt = format_kline_summary(ka)
        if kline_txt:
            lines.append("📊 <b>K线结构</b>")
            for line in kline_txt.split("\n"):
                lines.append(f"  {line}")
            lines.append("")

    # 价格
    _add_price_section(lines, agg)

    # 确认信号
    if conf:
        lines.append("📌 <b>确认信号</b>（仅参考）")
        for r in conf[:3]:
            lines.append(f"• {r['detail']}")
        lines.append("")

    # 操作参考
    if entry:
        _add_entry_section(lines, entry, direction="LONG")

    ts = time.strftime("%H:%M UTC", time.gmtime())
    lines += [
        f"🕐 {ts}  ·  延迟约15秒",
        f"➜ <code>/analyze {token}</code> 深度分析 + K线图",
    ]
    return "\n".join(lines)


# ────────────────────────────────────────
# HIGH ALERT — 做空预警
# ────────────────────────────────────────

def fmt_short_alert(result: dict, first_alert_ago_min: int = 0,
                    price_change_since_alert: float = 0) -> str:
    token  = result["token"]
    prob   = result["short_probability"]
    phase  = result["phase"]
    agg    = result["agg"]
    st     = result["short_triggered"]
    entry  = result.get("entry_zone", {})
    clf    = result.get("classification", {})

    # 浮动警告级别
    float_ratio = clf.get("float_ratio", 0.5)
    low_float_warning = float_ratio < 0.30

    prob_icon = _prob_icon(prob)
    lines = [
        f"🔻 <b>{token}/USDT</b>  做空概率 <b>{prob*100:.0f}%</b>",
        f"{prob_icon} {phase}",
        f"{format_behavior_tag(clf)}",
    ]

    if first_alert_ago_min > 0:
        lines.append(
            f"⏱ 首次异动：{first_alert_ago_min}分钟前  "
            f"已{'涨' if price_change_since_alert >= 0 else '跌'}"
            f" {abs(price_change_since_alert)*100:.1f}%"
        )
    lines.append("")

    # 低流通做空风险警告
    if low_float_warning:
        lines += [
            "🚨 <b>高风险警告</b>",
            f"该代币流通量极低（{float_ratio*100:.1f}%）",
            "做市商可随时发动逼空，做空被爆仓风险极高",
            "建议：仓位≤总资金5%，严格止损，负费率时禁止做空",
            "",
        ]

    # 做空信号
    if st:
        lines.append("🔻 <b>做空信号</b>")
        for r in sorted(st, key=lambda x: x.get("score", 0), reverse=True)[:5]:
            lines.append(f"⬇️ {r['detail']}")
        lines.append("")

    # K线结构
    ka = agg.get("kline_analysis", {}).get("binance", {})
    if ka:
        kline_txt = format_kline_summary(ka)
        if kline_txt:
            lines.append("📊 <b>K线结构</b>")
            for line in kline_txt.split("\n"):
                lines.append(f"  {line}")
            lines.append("")

    # 价格
    _add_price_section(lines, agg)

    # 操作参考（做空）
    if entry:
        _add_entry_section(lines, entry, direction="SHORT")

    ts = time.strftime("%H:%M UTC", time.gmtime())
    lines += [
        f"🕐 {ts}  ·  延迟约15秒",
        f"➜ <code>/analyze {token}</code> 深度分析",
    ]
    return "\n".join(lines)


# ────────────────────────────────────────
# MEDIUM ALERT — 批量汇总
# ────────────────────────────────────────

def fmt_medium_batch(results: list) -> str:
    if not results:
        return ""
    long_results  = [r for r in results if r["direction"] == "LONG"]
    short_results = [r for r in results if r["direction"] == "SHORT"]
    neutral       = [r for r in results if r["direction"] == "NEUTRAL"]

    lines = ["📡 <b>预警汇总</b>", ""]

    if long_results:
        lines.append("📈 <b>做多候选</b>")
        for r in long_results:
            prob = r["long_probability"]
            clf  = r.get("classification", {})
            blabel = clf.get("behavior_label", "")
            lines.append(
                f"  🟢 <b>{r['token']}</b>  {prob*100:.0f}%  {blabel}\n"
                f"     {r['phase']}\n"
                f"     ➜ <code>/analyze {r['token']}</code>"
            )
        lines.append("")

    if short_results:
        lines.append("📉 <b>做空候选</b>")
        for r in short_results:
            prob = r["short_probability"]
            clf  = r.get("classification", {})
            blabel = clf.get("behavior_label", "")
            lines.append(
                f"  🔴 <b>{r['token']}</b>  {prob*100:.0f}%  {blabel}\n"
                f"     {r['phase']}\n"
                f"     ➜ <code>/analyze {r['token']}</code>"
            )
        lines.append("")

    if neutral:
        lines.append("⚫ <b>信号混合（观望）</b>")
        for r in neutral[:3]:
            lines.append(f"  • {r['token']}  {r['phase']}")
        lines.append("")

    ts = time.strftime("%H:%M UTC", time.gmtime())
    lines.append(f"🕐 {ts}")
    return "\n".join(lines)


# ────────────────────────────────────────
# 每日汇总（取代每轮扫描总结）
# ────────────────────────────────────────

def fmt_daily_summary(stats: dict) -> str:
    """
    每日 00:00 UTC 推送一次
    包含昨日统计和命中率
    """
    ts = time.strftime("%Y-%m-%d UTC", time.gmtime())
    lines = [
        f"📊 <b>每日汇报</b>  {ts}",
        "",
        f"昨日预警：{stats.get('total_alerts', 0)}次",
        f"  📈 做多预警：{stats.get('long_alerts', 0)}次",
        f"  📉 做空预警：{stats.get('short_alerts', 0)}次",
        "",
    ]

    # 命中率（如果有backtest数据）
    long_wr  = stats.get("long_win_rate")
    short_wr = stats.get("short_win_rate")

    if long_wr is not None:
        lines += [
            "🎯 <b>昨日命中率</b>（4小时内涨/跌5%+）",
            f"  做多：{long_wr*100:.1f}%  （{stats.get('long_wins',0)}/{stats.get('long_total',0)}）",
            f"  做空：{short_wr*100:.1f}%  （{stats.get('short_wins',0)}/{stats.get('short_total',0)}）",
            "",
        ]

    # 最活跃代币
    top_tokens = stats.get("top_tokens", [])
    if top_tokens:
        lines.append("🏆 <b>昨日最活跃</b>")
        for i, t in enumerate(top_tokens[:3], 1):
            lines.append(f"  {i}. {t['token']}  预警{t['count']}次")
        lines.append("")

    lines.append("系统持续运行中 ✅")
    return "\n".join(lines)


# ────────────────────────────────────────
# Agent 层深度分析报告
# ────────────────────────────────────────

def fmt_analysis_report(token: str, result: dict,
                         llm_text: str,
                         realtime: dict = None) -> str:
    direction = result.get("direction", "NEUTRAL")
    phase     = result["phase"]
    agg       = result["agg"]
    entry     = result.get("entry_zone", {})
    clf       = result.get("classification", {})
    ts        = time.strftime("%H:%M:%S UTC", time.gmtime())

    if direction == "LONG":
        prob = result["long_probability"]
        icon = "📈"
        prob_label = f"做多概率 <b>{prob*100:.0f}%</b>"
    elif direction == "SHORT":
        prob = result["short_probability"]
        icon = "📉"
        prob_label = f"做空概率 <b>{prob*100:.0f}%</b>"
    else:
        prob = max(result.get("long_probability", 0),
                   result.get("short_probability", 0))
        icon = "⚫"
        prob_label = f"信号混合  概率{prob*100:.0f}%"

    lines = [
        f"{icon} <b>{token}/USDT 深度分析</b>",
        f"{prob_label}  ·  {phase}",
        f"{format_behavior_tag(clf)}",
        f"生成时间：{ts}",
        "",
        "💹 <b>实时价格</b>",
    ]

    # 价格
    outlier  = agg.get("futures_outlier", "")
    spot_avg = agg.get("spot_avg", 0)
    fp       = agg.get("futures_prices", {})
    basis    = agg.get("basis", {})
    max_basis= agg.get("max_basis", 0)
    max_b_ex = agg.get("max_basis_ex", "")

    if realtime:
        for ex in EXCHANGES:
            rt = realtime.get(ex, {})
            bt = rt.get("futures_bt")
            if bt:
                mid  = bt.get("mid", 0)
                flag = "⚠️" if ex == outlier else "  "
                lines.append(f"{flag} {ex.capitalize():<8} ${_fmt_p(mid)}")
    else:
        for ex in EXCHANGES:
            p = fp.get(ex)
            if p:
                flag = "⚠️" if ex == outlier else "  "
                lines.append(f"{flag} {ex.capitalize():<8} ${_fmt_p(p)}")

    if spot_avg:
        lines.append(f"  {'现货均价':<8} ${_fmt_p(spot_avg)}")
    if max_basis > 0.003:
        b = basis.get(max_b_ex, 0)
        d = "溢价" if b > 0 else "折价"
        lines.append(f"  ⚠️ 基差{max_b_ex} {d}{max_basis*100:.2f}%")

    # 操作参考
    if entry:
        lines.append("")
        _add_entry_section(lines, entry, entry.get("direction", direction))

    lines += [
        "",
        "─" * 33,
        "🤖 <b>AI 深度分析</b>",
        "",
        llm_text,
        "",
        "─" * 33,
        "⚠️ 本报告仅供参考，不构成投资建议",
    ]
    return "\n".join(lines)


# ────────────────────────────────────────
# 系统消息
# ────────────────────────────────────────

def fmt_system(msg_type: str, detail: str = "") -> str:
    icons = {
        "startup":    "⚙️ 上涨/下跌预警系统已启动",
        "twap_ready": "✅ TWAP检测已激活",
        "error":      "❌ 系统异常",
        "heartbeat":  "💓 系统运行正常",
    }
    base = icons.get(msg_type, msg_type)
    return f"{base}\n{detail}" if detail else base


# ────────────────────────────────────────
# 内部工具函数
# ────────────────────────────────────────

def _entry_advice(first_alert_ago_min: int,
                   price_change: float,
                   phase: str) -> str:
    """根据已启动时长和涨幅给出入场建议"""

    # 已大幅拉升，不建议追高
    if price_change > 0.15:
        return "❌ 不建议追高（已涨超15%，风险回报比极差）"

    if price_change > 0.08:
        return "⚠️ 谨慎（已涨8%+，等待回踩再考虑）"

    # 尚在早期
    if first_alert_ago_min < 30 and price_change < 0.05:
        return "✅ 可考虑介入（早期阶段，涨幅有限）"

    if "建仓" in phase or "蓄力" in phase:
        return "✅ 适合布局（尚未启动，前置信号明确）"

    if "逼空进行中" in phase:
        return "⚠️ 谨慎（逼空已启动，追高需严格止损）"

    if "出货" in phase:
        return "❌ 不建议做多（出货信号，考虑做空）"

    return "⚠️ 观望（等待信号更明确）"


def _add_price_section(lines: list, agg: dict):
    fp       = agg.get("futures_prices", {})
    spot_avg = agg.get("spot_avg", 0)
    outlier  = agg.get("futures_outlier", "")
    devs     = agg.get("futures_deviations", {})
    spread   = agg.get("max_futures_spread", 0)
    basis    = agg.get("basis", {})
    max_basis= agg.get("max_basis", 0)
    max_b_ex = agg.get("max_basis_ex", "")

    lines.append("💹 <b>价格对比</b>")
    for ex in EXCHANGES:
        p = fp.get(ex)
        if p is None:
            continue
        flag    = "⚠️" if ex == outlier else "  "
        dev     = devs.get(ex, 0)
        dev_str = f" {dev*100:+.2f}%" if ex == outlier else ""
        lines.append(f"{flag} {ex.capitalize():<8} ${_fmt_p(p)}{dev_str}")
    if spot_avg:
        lines.append(f"  {'现货均价':<8} ${_fmt_p(spot_avg)}")
    if spread > 0.003:
        lines.append(f"  合约价差 <b>{spread*100:.2f}%</b> 🔴")
    if max_basis > 0.005:
        b    = basis.get(max_b_ex, 0)
        bdir = "溢价" if b > 0 else "折价"
        flag = "🚨" if max_basis > 0.05 else "🔴"
        lines.append(f"  {flag} 基差{max_b_ex} {bdir}{max_basis*100:.2f}%")
    lines.append("")


def _add_entry_section(lines: list, entry: dict, direction: str):
    lines.append("🎯 <b>操作参考</b>")

    if direction == "LONG":
        el = entry.get("entry_low", 0)
        eh = entry.get("entry_high", 0)
        sl = entry.get("stop_loss", 0)
        t1 = entry.get("target_1", 0)
        t2 = entry.get("target_2", 0)
        w  = entry.get("window", "")
        if el and eh:
            lines.append(f"做多区间：${_fmt_p(el)} ~ ${_fmt_p(eh)}")
        if t1:
            up1 = (t1 - eh) / eh * 100 if eh else 0
            lines.append(f"目标一：${_fmt_p(t1)}（+{up1:.1f}%）")
        if t2:
            up2 = (t2 - eh) / eh * 100 if eh else 0
            lines.append(f"目标二：${_fmt_p(t2)}（+{up2:.1f}%）")
        if sl:
            down = (sl - eh) / eh * 100 if eh else 0
            lines.append(f"止损：${_fmt_p(sl)}（{down:.1f}%）")
        if w:
            lines.append(f"预计窗口：{w}")

    elif direction == "SHORT":
        el = entry.get("entry_low", 0)
        eh = entry.get("entry_high", 0)
        sl = entry.get("stop_loss", 0)
        t1 = entry.get("target_1", 0)
        t2 = entry.get("target_2", 0)
        w  = entry.get("window", "")
        warning = entry.get("warning", "")
        if el and eh:
            lines.append(f"做空区间：${_fmt_p(el)} ~ ${_fmt_p(eh)}")
        if t1:
            down1 = (t1 - eh) / eh * 100 if eh else 0
            lines.append(f"目标一：${_fmt_p(t1)}（{down1:.1f}%）")
        if t2:
            down2 = (t2 - eh) / eh * 100 if eh else 0
            lines.append(f"目标二：${_fmt_p(t2)}（{down2:.1f}%）")
        if sl:
            up = (sl - eh) / eh * 100 if eh else 0
            lines.append(f"止损：${_fmt_p(sl)}（+{up:.1f}%）")
        if w:
            lines.append(f"预计窗口：{w}")
        if warning:
            lines.append(f"\n{warning}")

    lines.append("")


def _prob_icon(prob: float) -> str:
    if   prob >= 0.80: return "🔴"
    elif prob >= 0.70: return "🟠"
    elif prob >= 0.60: return "🟡"
    else:              return "⚪"


def _fmt_p(p: float) -> str:
    if   p >= 1000:  return f"{p:,.2f}"
    elif p >= 1:     return f"{p:.4f}"
    elif p >= 0.001: return f"{p:.6f}"
    else:            return f"{p:.8f}"
