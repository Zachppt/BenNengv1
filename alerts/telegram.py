# ============================================================
# alerts/telegram.py — 推送层 v2
# 新增：现货基差展示、新操纵模式标注
# ============================================================

import aiohttp
import logging
import time
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)
API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

EXCHANGES = ["binance", "okx", "bybit", "bitget"]


# ────────────────────────────────────────
# 发送
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
                    logger.error(f"Telegram 推送失败: {r.status} {body}")
                    return False
                return True
    except Exception as e:
        logger.error(f"Telegram 推送异常: {e}")
        return False


# ────────────────────────────────────────
# HIGH ALERT 格式（精简，脚本层推送）
# ────────────────────────────────────────

def fmt_high_alert(result: dict, first_alert_ago_min: int = 0) -> str:
    token    = result["token"]
    score    = result["score"]
    phase    = result["phase"]
    agg      = result["agg"]
    triggered= result["triggered"]

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
    depths   = agg.get("depths", {})

    lines = [
        f"🚨 <b>{token}/USDT</b>  评分 {score}  {phase}",
        f"首次异动：{first_alert_ago_min}分钟前" if first_alert_ago_min else "本轮首次",
        "",
    ]

    # ── 跨所价格 + 现货基差 ──
    lines.append("💹 <b>跨所价格</b>")
    for ex in EXCHANGES:
        p = fp.get(ex)
        if p is None:
            continue
        flag = "⚠️" if ex == outlier else "  "
        dev  = devs.get(ex, 0)
        dev_str = f" {dev*100:+.2f}%" if ex == outlier else ""
        lines.append(f"{flag} {ex.capitalize():<8} ${_fmt_price(p)}{dev_str}")

    # 现货价格
    if spot_avg > 0:
        lines.append(f"  {'现货均价':<8} ${_fmt_price(spot_avg)}")

    if spread > 0.003:
        lines.append(f"  合约价差 <b>{spread*100:.2f}%</b>  🔴")

    # 基差告警（新增）
    if max_basis > 0.003:
        b_pct = basis.get(max_b_ex, 0)
        direction = "合约溢价" if b_pct > 0 else "合约折价"
        flag = "🚨" if max_basis > 0.05 else "🔴" if max_basis > 0.01 else "⚠️"
        lines.append(
            f"  {flag} <b>基差 {max_b_ex} {direction}{max_basis*100:.2f}%"
            f" — 合约{'严重' if max_basis > 0.05 else ''}脱离现货</b>"
        )
    lines.append("")

    # ── OI 分布 ──
    lines.append("📊 <b>OI 分布</b>")
    for ex in EXCHANGES:
        oi = ois.get(ex)
        if oi is None:
            continue
        share = shares.get(ex, 0)
        flag  = "🔴" if share > 0.45 else "  "
        lines.append(
            f"{flag} {ex.capitalize():<8} "
            f"${oi/1e6:.1f}M  {share*100:.1f}%"
        )
    lines.append(f"  {'合计':<8} ${total_oi/1e6:.1f}M")
    lines.append("")

    # ── 资金费率 ──
    lines.append("💰 <b>资金费率</b>")
    for ex in EXCHANGES:
        fr = fundings.get(ex)
        if fr is None:
            continue
        dev = fr - fund_mean
        flag = "🔴" if abs(dev) > 0.0004 else "  "
        lines.append(f"{flag} {ex.capitalize():<8} {fr*100:+.4f}%")
    lines.append("")

    # ── 订单簿 ──
    lines.append("📖 <b>订单簿</b>")
    for ex in EXCHANGES:
        imb = imbs.get(ex)
        if imb is None:
            continue
        dep = depths.get(ex, {})
        bid_d = dep.get("bid_depth_usd", 0)
        ask_d = dep.get("ask_depth_usd", 0)
        flag = "🚨" if imb > 0.7 else "🔴" if imb > 0.5 else "⚠️" if imb > 0.4 else "  "
        lines.append(
            f"{flag} {ex.capitalize():<8} "
            f"失衡{imb:.2f}  "
            f"买${bid_d/1e6:.2f}M 卖${ask_d/1e6:.2f}M"
        )
    lines.append("")

    # ── 触发规则（最多 5 条）──
    lines.append("⚡ <b>触发规则</b>")
    for r in triggered[:5]:
        icon = "🚨" if r["level"] == "L1" else "🔴" if r["level"] == "L2" else "⚠️"
        lines.append(f"{icon} {r['detail']}")
    if len(triggered) > 5:
        lines.append(f"   ... 另有 {len(triggered)-5} 条规则触发")
    lines.append("")

    # ── 底部 ──
    ts = time.strftime("%H:%M:%S UTC", time.gmtime())
    lines += [
        f"🕐 采集 {ts}  ·  延迟约 15~40 秒",
        f"➜ 回复 <code>/analyze {token}</code> 获取深度分析",
    ]

    return "\n".join(lines)


# ────────────────────────────────────────
# MEDIUM ALERT 批量汇总
# ────────────────────────────────────────

def fmt_medium_batch(results: list) -> str:
    if not results:
        return ""
    lines = ["🔴 <b>MEDIUM ALERT 汇总</b>", ""]
    for r in results:
        top = sorted(r["triggered"], key=lambda x: x["score"], reverse=True)
        detail = top[0]["detail"] if top else ""
        lines.append(
            f"🔴 <b>{r['token']}</b>  评分{r['score']}  {r['phase']}\n"
            f"   {detail}\n"
            f"   ➜ <code>/analyze {r['token']}</code>"
        )
        lines.append("")
    ts = time.strftime("%H:%M:%S UTC", time.gmtime())
    lines.append(f"🕐 {ts}")
    return "\n".join(lines)


# ────────────────────────────────────────
# 扫描总结
# ────────────────────────────────────────

def fmt_scan_summary(high: int, medium: int, total: int,
                     elapsed: float, next_min: int,
                     coldstart_tokens: list = None) -> str:
    ts   = time.strftime("%H:%M UTC", time.gmtime())
    nxt  = time.strftime("%H:%M UTC", time.gmtime(time.time() + next_min * 60))
    lines = [
        f"📊 <b>扫描完成</b>  {ts}",
        f"扫描 {total} 个合约  耗时 {elapsed:.0f}秒",
        f"🚨 HIGH {high}  🔴 MEDIUM {medium}",
        f"下次扫描 {nxt}",
    ]
    if coldstart_tokens:
        lines.append(f"⚙️ TWAP冷启动中: {', '.join(coldstart_tokens[:3])}")
    if high == 0 and medium == 0:
        lines.append("✅ 市场平静，无异动")
    return "\n".join(lines)


# ────────────────────────────────────────
# Agent 层深度分析报告
# ────────────────────────────────────────

def fmt_analysis_report(token: str, result: dict,
                         llm_text: str,
                         realtime: dict = None) -> str:
    """
    /analyze 触发后的完整报告
    realtime: fetch_token_realtime() 的返回（即时 Bid/Ask + 现货价格）
    """
    score  = result["score"]
    phase  = result["phase"]
    agg    = result["agg"]
    level  = result["level"]

    icon = "🚨" if level == "HIGH" else "🔴" if level == "MEDIUM" else "⚠️"
    ts   = time.strftime("%H:%M:%S UTC", time.gmtime())

    lines = [
        f"{icon} <b>{token}/USDT 深度分析报告</b>",
        f"评分 {score}  ·  {phase}",
        f"生成时间：{ts}",
        "",
    ]

    # ── 即时价格（实时拉取，最新）──
    lines.append("💹 <b>实时价格（刚刚拉取）</b>")
    if realtime:
        outlier = agg.get("futures_outlier", "")
        spot_avg = agg.get("spot_avg", 0)
        for ex in EXCHANGES:
            rt = realtime.get(ex, {})
            bt = rt.get("futures_bt")
            sp = rt.get("spot")
            if bt:
                mid  = bt.get("mid", 0)
                flag = "⚠️" if ex == outlier else "  "
                lines.append(f"{flag} {ex.capitalize():<8} 合约 ${_fmt_price(mid)}")
            if sp:
                sp_price = sp.get("price", 0) if isinstance(sp, dict) else sp
                lines.append(f"  {ex.capitalize():<8} 现货 ${_fmt_price(sp_price)}")

        # 基差汇总
        basis = agg.get("basis", {})
        max_b = agg.get("max_basis", 0)
        max_b_ex = agg.get("max_basis_ex", "")
        if max_b > 0.003:
            b = basis.get(max_b_ex, 0)
            d = "溢价" if b > 0 else "折价"
            lines.append(f"  ⚠️ 最大基差 {max_b_ex} {d}{max_b*100:.2f}%")
    else:
        fp = agg.get("futures_prices", {})
        sp = agg.get("spot_prices", {})
        for ex in EXCHANGES:
            p = fp.get(ex)
            if p:
                lines.append(f"  {ex.capitalize():<8} ${_fmt_price(p)}")

    lines += ["", "─" * 33, "🤖 <b>AI 深度分析</b>", "", llm_text, "",
              "─" * 33,
              "⚠️ 即时价格已实时拉取；分析基于缓存趋势数据",
              "⚠️ 本报告仅供参考，不构成投资建议"]

    return "\n".join(lines)


# ────────────────────────────────────────
# 系统消息
# ────────────────────────────────────────

def fmt_system(msg_type: str, detail: str = "") -> str:
    icons = {
        "startup":    "⚙️ 妖币监控系统已启动",
        "twap_ready": "✅ TWAP 检测已激活（历史快照充足）",
        "error":      "❌ 系统异常",
        "coldstart":  "🔄 冷启动期，正在积累历史快照",
        "heartbeat":  "💓 系统运行正常",
    }
    base = icons.get(msg_type, msg_type)
    return f"{base}\n{detail}" if detail else base


# ────────────────────────────────────────
# 工具函数
# ────────────────────────────────────────

def _fmt_price(p: float) -> str:
    """根据价格大小自动选择小数位"""
    if p >= 1000:
        return f"{p:,.2f}"
    elif p >= 1:
        return f"{p:.4f}"
    elif p >= 0.001:
        return f"{p:.6f}"
    else:
        return f"{p:.8f}"
