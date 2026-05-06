# ============================================================
# agent/analyzer.py — Agent 层 v2
# 读取缓存 → 补充即时数据 → 调用 LLM → 推送报告
# ============================================================

import asyncio
import aiohttp
import json
import logging
import time
import os

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, SCAN_RESULT_PATH
from data.fetcher import fetch_token_realtime, fetch_token_full
from rules.engine import RuleEngine, aggregate
from cache.snapshot import (
    get_snapshots, build_history_summary,
    get_alert_history, is_coldstart_done,
)
from alerts.telegram import send, fmt_analysis_report, fmt_system

logger = logging.getLogger(__name__)

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
TELEGRAM_API  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
engine        = RuleEngine()


# ============================================================
# LLM 调用
# ============================================================

async def call_llm(system_prompt: str, user_prompt: str) -> str:
    headers = {
        "Content-Type":      "application/json",
        "anthropic-version": "2023-06-01",
    }
    payload = {
        "model":      "claude-sonnet-4-20250514",
        "max_tokens": 1000,
        "system":     system_prompt,
        "messages":   [{"role": "user", "content": user_prompt}],
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                ANTHROPIC_API, headers=headers, json=payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    return data["content"][0]["text"]
                body = await r.text()
                logger.error(f"LLM 调用失败: {r.status} {body}")
                return "LLM 分析暂时不可用，以下为规则引擎结果。"
    except Exception as e:
        logger.error(f"LLM 调用异常: {e}")
        return f"LLM 调用异常: {e}"


# ============================================================
# Prompt 构建
# ============================================================

SYSTEM_PROMPT = """你是一个加密衍生品市场操纵行为分析专家，专注于识别做市商操纵手法。

分析原则：
1. 不重新计算数值，直接基于提供的数据进行推理
2. 结论必须有具体数据支撑，不能泛泛而谈
3. 对不确定的部分如实说明，不猜测
4. 输出使用中文，总字数控制在 300 字以内
5. 格式严格按照要求输出，不添加多余内容"""


def build_prompt(token: str, result: dict, history: dict) -> str:
    triggered = result.get("triggered", [])
    score     = result.get("score", 0)
    phase     = result.get("phase", "未知")
    agg       = result.get("agg", {})

    # 关键数值
    futures_spread = agg.get("max_futures_spread", 0)
    outlier        = agg.get("futures_outlier", "无")
    max_basis      = agg.get("max_basis", 0)
    max_basis_ex   = agg.get("max_basis_ex", "无")
    basis_dict     = agg.get("basis", {})
    basis_dir      = ("合约溢价" if basis_dict.get(max_basis_ex, 0) > 0
                      else "合约折价")
    fund_mean      = agg.get("funding_mean", 0)
    fundings       = agg.get("fundings", {})
    oi_shares      = agg.get("oi_shares", {})
    imbalances     = agg.get("imbalances", {})

    tr = (agg.get("raw", {}).get("binance", {})
          .get("futures", {}).get("taker_ratio") or {})
    taker_cur = tr.get("current", 1.0)

    # 格式化触发规则
    rules_str = "\n".join(
        f"- [L{r['level'][-1]}] {r['detail']}"
        for r in triggered
    ) or "无明显异常"

    # 格式化历史趋势
    hist_str = "\n".join(
        f"{k}: {v}"
        for k, v in history.items()
    ) or "历史数据不足"

    # 格式化资金费率
    funding_detail = "  ".join(
        f"{ex}:{v*100:+.4f}%"
        for ex, v in fundings.items()
    )

    # 格式化 OI 集中度
    oi_share_str = "  ".join(
        f"{ex}:{v*100:.1f}%"
        for ex, v in oi_shares.items()
    )

    # 格式化失衡度
    imb_str = "  ".join(
        f"{ex}:{v:.2f}"
        for ex, v in imbalances.items()
    )

    return f"""以下是 {token}/USDT 的异常检测报告，请进行分析。

════════════════════════════════
异动评分：{score}
当前阶段（规则判断）：{phase}
════════════════════════════════

【触发规则】
{rules_str}

【关键数值】
跨所合约价差：{futures_spread*100:.2f}%（异常方：{outlier}）
现货-合约基差：{max_basis*100:.2f}%（{basis_dir}，{max_basis_ex}）
全平台资金费率均值：{fund_mean*100:.4f}%
各所资金费率：{funding_detail}
OI 集中度：{oi_share_str}
订单簿失衡度：{imb_str}
Taker 买卖比：{taker_cur:.4f}（当前）

【历史趋势（过去 2 小时）】
{hist_str}

════════════════════════════════
请按以下格式输出，不添加其他内容：

**阶段判断**
[一句话，从以下选择：建仓中 / 逼空蓄力中 / 逼空进行中 / 逼空收尾 / 出货进行中 / 多空双杀 / 流动性猎杀 / 信号混合]

**主操纵场所**
[交易所名称 + 一句话说明为什么]

**操纵模式**
[从以下选择，可多选：MYX型逼空 / COAI型拉高出货 / TWAP建仓 / 洗盘刷量 / 跨所标记价格操纵 / 多空双杀 / 定点清算 / 流动性猎杀 / 现货合约背离 / 无法判断]

**风险评级**
操纵风险：[低 / 中 / 高 / 极高]
做多风险：[低 / 中 / 高 / 极高]
做空风险：[低 / 中 / 高 / 极高]

**核心结论**
[一句话，不超过 40 字，说明当前最重要的风险或机会]"""


# ============================================================
# 主分析函数
# ============================================================

async def analyze_token(token: str) -> str:
    """
    /analyze TOKEN 的完整处理流程：
    1. 读取缓存快照
    2. 读取 scan_result.json 当前异常标记
    3. 实时拉取即时价格
    4. 构建 Prompt → 调用 LLM
    5. 格式化报告
    """
    token = token.upper().strip().replace("USDT", "")
    logger.info(f"开始分析 {token}...")

    await send(f"⏳ 正在分析 <b>{token}/USDT</b>，请稍候（约 15 秒）...")

    try:
        async with aiohttp.ClientSession() as session:

            # Step 1：判断缓存状态
            snap_count = len(get_snapshots(token, "binance", limit=1))
            has_cache  = snap_count > 0

            # Step 2：获取规则结果
            if has_cache:
                # 从 scan_result.json 读取当前异常
                result = _read_result_from_cache(token)
                if not result:
                    # 缓存中有快照但 scan_result 没有该代币 → 实时计算
                    result = await _realtime_analyze(session, token)
            else:
                # 无缓存：实时采集并分析
                await send(
                    f"⚠️ {token} 未在监控列表，正在实时采集数据（无历史趋势）..."
                )
                result = await _realtime_analyze(session, token)

            if not result:
                return f"❌ 无法获取 {token} 数据，请确认代币名称是否正确"

            # Step 3：即时价格补充
            realtime = await fetch_token_realtime(session, token)

            # Step 4：历史摘要
            history = build_history_summary(token) if has_cache else {}

            # Step 5：调用 LLM
            prompt   = build_prompt(token, result, history)
            llm_text = await call_llm(SYSTEM_PROMPT, prompt)

            # Step 6：格式化
            report = fmt_analysis_report(
                token, result, llm_text, realtime
            )

            if not has_cache:
                report += "\n\n⚠️ 该代币已加入监控列表，下次分析将包含历史趋势"
                # 加入下次扫描候选（通过 alert_state）
                _add_to_watch(token)

            return report

    except Exception as e:
        logger.exception(f"分析 {token} 异常: {e}")
        return f"❌ 分析 {token} 时发生错误：{e}"


def _read_result_from_cache(token: str) -> dict | None:
    """从 scan_result.json 读取最新的规则结果"""
    if not os.path.exists(SCAN_RESULT_PATH):
        return None
    try:
        with open(SCAN_RESULT_PATH, encoding="utf-8") as f:
            data = json.load(f)
        for alert in data.get("high_alerts", []) + data.get("medium_alerts", []):
            if alert.get("token") == token:
                # 重新从快照构建完整 agg（scan_result 只存摘要）
                return _rebuild_result_from_snapshots(token, alert)
        return None
    except Exception:
        return None


def _rebuild_result_from_snapshots(token: str, alert: dict) -> dict:
    """
    scan_result.json 只存储摘要，
    从快照重建 agg 结构供 Prompt 构建使用
    """
    snaps = get_snapshots(token, "binance", limit=1)
    if not snaps:
        return alert

    snap = snaps[0]

    # 构建轻量 agg（只包含 Prompt 需要的字段）
    agg = {
        "max_futures_spread": snap.get("max_spread", 0),
        "futures_outlier":    None,
        "futures_deviations": {},
        "max_basis":          snap.get("max_basis", 0),
        "max_basis_ex":       None,
        "basis":              {},
        "spot_avg":           snap.get("spot_avg", 0),
        "spot_prices":        {},
        "futures_prices":     {"binance": snap.get("price", 0)},
        "ois":                {"binance": snap.get("oi_usd", 0)},
        "oi_shares":          {},
        "total_oi":           snap.get("total_oi", 0),
        "fundings":           {"binance": snap.get("funding", 0)},
        "funding_mean":       snap.get("funding", 0),
        "funding_devs":       {},
        "imbalances":         {"binance": snap.get("imbalance", 0)},
        "depths":             {},
        "klines":             {},
        "raw":                {},
    }

    return {
        "token":     token,
        "score":     alert.get("score", 0),
        "level":     alert.get("level", "MEDIUM"),
        "phase":     alert.get("phase", "⚫ 信号混合"),
        "triggered": alert.get("triggered", []),
        "agg":       agg,
    }


async def _realtime_analyze(session, token: str) -> dict | None:
    """无缓存时：实时采集完整数据并运行规则引擎"""
    raw = await fetch_token_full(session, token)
    if not raw:
        return None
    agg    = aggregate(token, raw)
    result = engine.run(token, agg, snapshot_fn=None, coldstart_done=False)
    return result


def _add_to_watch(token: str):
    """将未监控的代币加入 alert_state 的 hfreq_tokens"""
    from main import read_alert_state, write_alert_state
    try:
        state = read_alert_state()
        if token not in state.get("hfreq_tokens", []):
            state.setdefault("hfreq_tokens", []).append(token)
            write_alert_state(state)
    except Exception:
        pass


# ============================================================
# /history 指令处理
# ============================================================

def handle_history(token: str) -> str:
    token = token.upper().strip().replace("USDT", "")
    history = get_alert_history(token, hours=24)

    if not history:
        return f"📋 <b>{token}</b> 过去 24 小时无异动记录"

    lines = [f"📋 <b>{token}/USDT</b> 过去 24 小时异动记录", ""]

    prev_phase = None
    for h in history[:20]:   # 最多显示 20 条
        phase_mark = " ← 阶段切换" if h["phase"] != prev_phase and prev_phase else ""
        lines.append(
            f"{h['time_str']}  评分 {h['score']:>2}  "
            f"{h['phase']}{phase_mark}"
        )
        prev_phase = h["phase"]

    if len(history) > 20:
        lines.append(f"... 共 {len(history)} 条记录，仅显示最近 20 条")

    return "\n".join(lines)


# ============================================================
# /status 指令处理
# ============================================================

def handle_status() -> str:
    if not os.path.exists(SCAN_RESULT_PATH):
        return "⚠️ 扫描结果文件不存在，脚本可能尚未运行"

    try:
        with open(SCAN_RESULT_PATH, encoding="utf-8") as f:
            data = json.load(f)

        scan_ts  = data.get("scan_ts", 0)
        next_ts  = data.get("system", {}).get("next_scan_ts", 0)
        elapsed  = int((time.time() - scan_ts) / 60)
        hfreq    = data.get("system", {}).get("hfreq_tokens", [])
        cold     = data.get("system", {}).get("coldstart_done", False)
        high_ct  = len(data.get("high_alerts", []))
        med_ct   = len(data.get("medium_alerts", []))
        duration = int(data.get("duration_sec", 0))

        scan_time = time.strftime("%H:%M UTC", time.gmtime(scan_ts))
        next_time = time.strftime("%H:%M UTC", time.gmtime(next_ts))

        lines = [
            "⚙️ <b>系统状态</b>",
            f"上次扫描：{scan_time}（{elapsed} 分钟前，耗时 {duration}秒）",
            f"下次扫描：{next_time}",
            f"当前 HIGH：{high_ct} 个",
            f"当前 MEDIUM：{med_ct} 个",
            f"高频监控：{', '.join(hfreq) if hfreq else '无'}",
            f"TWAP检测：{'✅ 已激活' if cold else '🔄 冷启动中'}",
        ]

        if high_ct > 0:
            lines.append("")
            lines.append("🚨 <b>当前 HIGH ALERT</b>")
            for alert in data["high_alerts"]:
                lines.append(
                    f"  {alert['token']}  评分{alert['score']}  {alert['phase']}"
                )

        return "\n".join(lines)

    except Exception as e:
        return f"❌ 读取状态失败: {e}"


# ============================================================
# Telegram 轮询（处理用户指令）
# ============================================================

async def poll_commands():
    """轮询 Telegram 消息，处理用户指令"""
    offset = 0
    logger.info("开始监听 Telegram 指令...")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    f"{TELEGRAM_API}/getUpdates",
                    params={"offset": offset, "timeout": 30},
                    timeout=aiohttp.ClientTimeout(total=35),
                ) as r:
                    updates = await r.json()

                for update in updates.get("result", []):
                    offset = update["update_id"] + 1
                    msg    = update.get("message", {})
                    text   = (msg.get("text") or "").strip()

                    if not text:
                        continue

                    # 解析指令
                    if text.lower().startswith("/analyze "):
                        token = text[9:].strip()
                        asyncio.create_task(_handle_analyze(token))

                    elif text.lower() == "/status":
                        await send(handle_status())

                    elif text.lower().startswith("/history "):
                        token = text[9:].strip()
                        await send(handle_history(token))

                    elif text.lower().startswith("/mute "):
                        parts = text.split()
                        token = parts[1].upper() if len(parts) > 1 else ""
                        hours = 24
                        if len(parts) > 2:
                            dur = parts[2].lower()
                            hours = (1  if dur == "1h"
                                     else 6  if dur == "6h"
                                     else 168 if dur == "7d"
                                     else 24)
                        if token:
                            _mute_token(token, hours)
                            await send(
                                f"🔇 <b>{token}</b> 已静默 {hours} 小时"
                            )

                    elif text.lower().startswith("/watch "):
                        token = text[7:].strip().upper()
                        if token:
                            _add_to_watch(token)
                            await send(
                                f"👁 <b>{token}</b> 已加入高频监控（1分钟快照）"
                            )

                    elif text.lower() == "/help":
                        await send(_help_text())

                    # 自然语言识别
                    elif _is_analyze_intent(text):
                        token = _extract_token(text)
                        if token:
                            asyncio.create_task(_handle_analyze(token))
                        else:
                            await send(_current_alerts_summary())

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Telegram 轮询异常: {e}")
                await asyncio.sleep(5)


async def _handle_analyze(token: str):
    report = await analyze_token(token)
    await send(report)


def _mute_token(token: str, hours: int):
    from main import read_alert_state, write_alert_state
    try:
        state = read_alert_state()
        state.setdefault("muted", {})[token] = int(
            time.time() + hours * 3600
        )
        write_alert_state(state)
    except Exception:
        pass


def _is_analyze_intent(text: str) -> bool:
    """自然语言意图识别：是否想分析某个代币"""
    keywords = ["分析", "看一下", "异动", "拉盘", "逼空", "做市商",
                "资金费率", "基差", "插针", "双杀", "妖币", "怎么样"]
    return any(kw in text for kw in keywords)


def _extract_token(text: str) -> str:
    """从自然语言中提取代币名称"""
    import re
    # 匹配大写字母或后面跟 USDT 的词
    matches = re.findall(r'\b([A-Z]{2,10})(USDT)?\b', text.upper())
    # 过滤常见非代币词
    stopwords = {"USDT", "THE", "FOR", "AND", "ARE", "YOU", "NOT"}
    for m in matches:
        token = m[0]
        if token not in stopwords and len(token) >= 2:
            return token
    return ""


def _current_alerts_summary() -> str:
    """返回当前所有 HIGH ALERT 的简要列表"""
    if not os.path.exists(SCAN_RESULT_PATH):
        return "⚠️ 暂无扫描数据"
    try:
        with open(SCAN_RESULT_PATH, encoding="utf-8") as f:
            data = json.load(f)
        highs = data.get("high_alerts", [])
        if not highs:
            return "✅ 当前无 HIGH ALERT 代币"
        lines = ["🚨 <b>当前异动代币</b>", ""]
        for h in highs:
            lines.append(
                f"• <b>{h['token']}</b>  评分{h['score']}  {h['phase']}\n"
                f"  /analyze {h['token']}"
            )
        return "\n".join(lines)
    except Exception:
        return "❌ 读取数据失败"


def _help_text() -> str:
    return (
        "📖 <b>妖币监控系统指令</b>\n\n"
        "/analyze TOKEN  深度分析（约 15 秒）\n"
        "/status         系统运行状态\n"
        "/history TOKEN  过去 24 小时异动记录\n"
        "/mute TOKEN [时长]  静默预警（1h/6h/24h/7d）\n"
        "/watch TOKEN    加入高频监控（1 分钟快照）\n"
        "/help           显示本帮助\n\n"
        "自然语言示例：\n"
        "「分析一下 TRIA」\n"
        "「COAI 有没有异动」\n"
        "「现在有什么妖币」"
    )
