# ============================================================
# alerts/telegram.py — v3
# 概率制推送格式，含K线结构描述
# ============================================================

import aiohttp
import logging
import time
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from rules.kline_analyzer import format_kline_summary

logger = logging.getLogger(__name__)
API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
EXCHANGES = ["binance", "okx", "bybit", "bitget"]


# ────────────────────────────────────────
# 发送文字
# ────────────────────────────────────────

async def send(text: str, parse_mode: str = "HTML") -> bool:
    payload = {
        "chat_id":                  TELEGRAM_CHAT_ID,
        "text":                     text,
        "parse_mode":               parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{API}/sendMessage", json=payload,
                              timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    body = await r.text()
                    logger.error(f"Telegram推送失败: {r.status} {body}")
                    return False
                return True
    except Exception as e:
        logger.error(f"Telegram推送异常: {e}")
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
                if r.status != 200:
                    body = await r.text()
                    logger.error(f"图片推送失败: {r.status} {body}")
                    return False
                return True
    except Exception as e:
        logger.error(f"图片推送异常: {e}")
        return False


# ────────────────────────────────────────
# HIGH ALERT 精简预警（脚本层推送）
# ────────────────────────────────────────

def fmt_high_alert(result: dict,
                   first_alert_ago_min: int = 0) -> str:
    token    = result["token"]
    prob     = result["probability"]
    phase    = result["phase"]
    agg      = result["agg"]
    triggered= result["triggered"]
    entry    = result.get("entry_zone", {})

    fp       = agg.get("futures_prices", {})
    sp       = agg.get("spot_prices", {})
    spot_avg = agg.get("spot_avg", 0)
    basis    = agg.get("basis", {})
    max_basis= agg.get("max_basis", 0)
    max_b_ex = agg.get("max_basis_ex", "")
    devs     = agg.get("futures_deviations", {})
    outlier  = agg.get("futures_outlier", "")
    spread   = agg.get("max_futures_spread", 0)
    ois      = agg.get("ois", {})
    total_oi = agg.get("total_oi", 0)
    shares   = agg.get("oi_shares", {})
    fundings = agg.get("fundings", {})
    fund_mean= agg.get("funding_mean", 0)
    imbs     = agg.get("imbalances", {})

    # K线分析
    ka       = agg.get("kline_analysis", {}).get("binance", {})
    kline_txt= format_kline_summary(ka) if ka else ""

    # 分离前置信号和确认信号
    from config import SIGNAL_WEIGHTS
    HIGH_WEIGHT = 7
    pre_signals  = []   # 前置信号（权重高）
    conf_signals = []   # 确认信号（权重低）
    kline_signals= []   # K线信号

    for r in triggered:
        w = SIGNAL_WEIGHTS.get(r["rule"], 0)
        if r.get("source") == "kline":
            kline_signals.append(r)
        elif w >= HIGH_WEIGHT:
            pre_signals.append((r, w))
        elif w > 0:
            conf_signals.append(r)

    pre_signals.sort(key=lambda x: x[1], reverse=True)

    # 概率图标
    prob_icon = ("🔴" if prob >= 0.80
                 else "🟠" if prob >= 0.70
                 else "🟡" if prob >= 0.60
                 else "⚪")

    lines = [
        f"📡 <b>{token}/USDT</b>  上涨概率 <b>{prob*100:.0f}%</b>",
        f"{prob_icon} {phase}",
        f"首次预警：{first_alert_ago_min}分钟前" if first_alert_ago_min else "本轮首次",
        "",
    ]

    # ── 前置信号 ──
    if pre_signals:
        lines.append("🎯 <b>前置信号（核心驱动）</b>")
        for r, w in pre_signals[:5]:
            lines.append(f"✅ {r['detail']}  <i>+{w}%</i>")
        lines.append("")

    # ── K线结构 ──
    if kline_txt:
        lines.append("📊 <b>K线结构</b>")
        for line in kline_txt.split("\n"):
            lines.append(f"  {line}")
        lines.append("")

    # ── 跨所价格 ──
    lines.append("💹 <b>价格对比</b>")
    for ex in EXCHANGES:
        p = fp.get(ex)
        if p is None:
            continue
        flag = "⚠️" if ex == outlier else "  "
        dev  = devs.get(ex, 0)
        dev_str = f" {dev*100:+.2f}%" if ex == outlier else ""
        lines.append(f"{flag} {ex.capitalize():<8} ${_fmt_p(p)}{dev_str}")

    if spot_avg > 0:
        lines.append(f"  {'现货均价':<8} ${_fmt_p(spot_avg)}")

    if spread > 0.003:
        lines.append(f"  合约价差 <b>{spread*100:.2f}%</b> 🔴")

    if max_basis > 0.005:
        b     = basis.get(max_b_ex, 0)
        bdir  = "溢价" if b > 0 else "折价"
        bflag = "🚨" if max_basis > 0.05 else "🔴" if max_basis > 0.01 else "⚠️"
        lines.append(f"  {bflag} 基差{max_b_ex} {bdir}{max_basis*100:.2f}%")
    lines.append("")

    # ── OI ──
    lines.append("📊 <b>OI 分布</b>")
    for ex in EXCHANGES:
        oi = ois.get(ex)
        if oi is None:
            continue
        share = shares.get(ex, 0)
        flag  = "🔴" if share > 0.45 else "  "
        lines.append(f"{flag} {ex.capitalize():<8} ${oi/1e6:.1f}M  {share*100:.1f}%")
    lines.append(f"  {'合计':<8} ${total_oi/1e6:.1f}M")
    lines.append("")

    # ── 资金费率 ──
    lines.append("💰 <b>资金费率</b>")
    for ex in EXCHANGES:
        fr = fundings.get(ex)
        if fr is None:
            continue
        dev  = fr - fund_mean
        flag = "🔴" if abs(dev) > 0.0004 else "  "
        lines.append(f"{flag} {ex.capitalize():<8} {fr*100:+.4f}%")
    lines.append("")

    # ── 确认信号（折叠展示）──
    if conf_signals:
        lines.append("📌 <b>确认信号</b>（仅参考）")
        for r in conf_signals[:3]:
            lines.append(f"• {r['detail']}")
        lines.append("")

    # ── 入场区间 ──
    if entry:
        lines.append("🎯 <b>操作参考</b>")
        el = entry.get("entry_low", 0)
        eh = entry.get("entry_high", 0)
        sl = entry.get("stop_loss", 0)
        t1 = entry.get("target_1", 0)
        t2 = entry.get("target_2", 0)
        w  = entry.get("window", "")

        if el and eh:
            lines.append(f"关注区间：${_fmt_p(el)} ~ ${_fmt_p(eh)}")
        if t1:
            upside1 = (t1 - eh) / eh * 100 if eh else 0
            lines.append(f"目标一：${_fmt_p(t1)}（+{upside1:.1f}%）")
        if t2 and t2 != t1:
            upside2 = (t2 - eh) / eh * 100 if eh else 0
            lines.append(f"目标二：${_fmt_p(t2)}（+{upside2:.1f}%）")
        if sl:
            downside = (sl - eh) / eh * 100 if eh else 0
            lines.append(f"止损参考：${_fmt_p(sl)}（{downside:.1f}%）")
        if w:
            lines.append(f"预计窗口：{w}")
        lines.append("")

    # ── 底部 ──
    ts = time.strftime("%H:%M:%S UTC", time.gmtime())
    lines += [
        f"🕐 {ts}  ·  延迟约15秒",
        f"➜ <code>/analyze {token}</code> 深度分析 + K线图",
    ]

    return "\n".join(lines)


# ────────────────────────────────────────
# MEDIUM ALERT 批量汇总
# ────────────────────────────────────────

def fmt_medium_batch(results: list) -> str:
    if not results:
        return ""
    lines = ["📡 <b>预警汇总</b>", ""]

    for r in results:
        prob  = r["probability"]
        phase = r["phase"]
        icon  = ("🟠" if prob >= 0.70 else "🟡")
        top   = sorted(r["triggered"],
                       key=lambda x: abs(x.get("score", 0)), reverse=True)
        detail= top[0]["detail"] if top else ""
        lines.append(
            f"{icon} <b>{r['token']}</b>  {prob*100:.0f}%  {phase}\n"
            f"   {detail}\n"
            f"   ➜ <code>/analyze {r['token']}</code>"
        )
        lines.append("")

    ts = time.strftime("%H:%M UTC", time.gmtime())
    lines.append(f"🕐 {ts}")
    return "\n".join(lines)


# ────────────────────────────────────────
# 扫描总结
# ────────────────────────────────────────

def fmt_scan_summary(high: int, medium: int, total: int,
                     elapsed: float, next_min: int,
                     coldstart_tokens: list = None,
                     market_avg_prob: float = 0) -> str:
    ts  = time.strftime("%H:%M UTC", time.gmtime())
    nxt = time.strftime("%H:%M UTC",
                        time.gmtime(time.time() + next_min * 60))
    lines = [
        f"📊 <b>扫描完成</b>  {ts}",
        f"扫描 {total} 个合约  耗时 {elapsed:.0f}秒",
        f"🔴 强预警 {high}  🟡 预警 {medium}",
        f"市场平均概率：{market_avg_prob*100:.1f}%",
        f"下次扫描 {nxt}",
    ]
    if coldstart_tokens:
        lines.append(f"⚙️ TWAP冷启动中: {', '.join(coldstart_tokens[:3])}")
    if high == 0 and medium == 0:
        lines.append("✅ 当前无明显上涨预警")
    return "\n".join(lines)


# ────────────────────────────────────────
# Agent 层深度分析报告
# ────────────────────────────────────────

def fmt_analysis_report(token: str, result: dict,
                         llm_text: str,
                         realtime: dict = None) -> str:
    prob   = result["probability"]
    phase  = result["phase"]
    agg    = result["agg"]
    entry  = result.get("entry_zone", {})
    ts     = time.strftime("%H:%M:%S UTC", time.gmtime())

    prob_icon = ("🔴" if prob >= 0.80 else "🟠" if prob >= 0.70
                 else "🟡" if prob >= 0.60 else "⚪")

    lines = [
        f"{prob_icon} <b>{token}/USDT 深度分析</b>",
        f"上涨概率 <b>{prob*100:.0f}%</b>  ·  {phase}",
        f"生成时间：{ts}",
        "",
        "💹 <b>实时价格</b>",
    ]

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
            sp = rt.get("spot")
            if bt:
                mid  = bt.get("mid", 0)
                flag = "⚠️" if ex == outlier else "  "
                lines.append(f"{flag} {ex.capitalize():<8} 合约 ${_fmt_p(mid)}")
            if sp:
                sp_p = sp.get("price", 0) if isinstance(sp, dict) else sp
                lines.append(f"  {ex.capitalize():<8} 现货 ${_fmt_p(sp_p)}")
    else:
        for ex in EXCHANGES:
            p = fp.get(ex)
            if p:
                lines.append(f"  {ex.capitalize():<8} ${_fmt_p(p)}")

    if max_basis > 0.003:
        b    = basis.get(max_b_ex, 0)
        bdir = "溢价" if b > 0 else "折价"
        lines.append(f"  ⚠️ 最大基差 {max_b_ex} {bdir}{max_basis*100:.2f}%")

    # 入场区间
    if entry:
        lines += ["", "🎯 <b>操作参考</b>"]
        el = entry.get("entry_low", 0)
        eh = entry.get("entry_high", 0)
        sl = entry.get("stop_loss", 0)
        t1 = entry.get("target_1", 0)
        t2 = entry.get("target_2", 0)
        w  = entry.get("window", "")
        if el and eh:
            lines.append(f"关注区间：${_fmt_p(el)} ~ ${_fmt_p(eh)}")
        if t1:
            lines.append(f"目标一：${_fmt_p(t1)}")
        if t2 and t2 != t1:
            lines.append(f"目标二：${_fmt_p(t2)}")
        if sl:
            lines.append(f"止损参考：${_fmt_p(sl)}")
        if w:
            lines.append(f"预计窗口：{w}")

    lines += [
        "",
        "─" * 33,
        "🤖 <b>AI 深度分析</b>",
        "",
        llm_text,
        "",
        "─" * 33,
        "⚠️ 即时价格已实时拉取；K线图见上方",
        "⚠️ 本报告仅供参考，不构成投资建议",
    ]

    return "\n".join(lines)


# ────────────────────────────────────────
# 系统消息
# ────────────────────────────────────────

def fmt_system(msg_type: str, detail: str = "") -> str:
    icons = {
        "startup":    "⚙️ 上涨预警系统已启动",
        "twap_ready": "✅ TWAP检测已激活",
        "error":      "❌ 系统异常",
        "heartbeat":  "💓 系统运行正常",
    }
    base = icons.get(msg_type, msg_type)
    return f"{base}\n{detail}" if detail else base


# ────────────────────────────────────────
# 工具函数
# ────────────────────────────────────────

def _fmt_p(p: float) -> str:
    if p >= 1000:
        return f"{p:,.2f}"
    elif p >= 1:
        return f"{p:.4f}"
    elif p >= 0.001:
        return f"{p:.6f}"
    else:
        return f"{p:.8f}"
