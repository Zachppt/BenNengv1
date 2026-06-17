# ============================================================
# rules/behavior_classifier.py — 行为分类系统 v4
# 自动识别代币当前的市场行为类型
# ============================================================

import statistics
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
# 行为类型定义
# ============================================================

BEHAVIOR_TYPES = {
    "REACTIVE":       "被动跟随",      # 跟随BTC/市场整体
    "SQUEEZE":        "逼空型",        # 做市商逼空散户
    "PUMP_DUMP":      "拉高出货型",    # 快速拉升后砸盘
    "TWAP_ACCUM":     "TWAP建仓型",   # 慢速隐秘建仓
    "MOMENTUM_RIDE":  "借势拉盘型",   # 借BTC上涨顺势拉
    "STEALTH_ACCUM":  "隐秘建仓型",   # 低波动悄悄吸筹
    "DISTRIBUTION":   "出货型",        # 做市商在出货
    "WASHOUT":        "洗盘型",        # 震仓清洗散户
    "STOP_HUNT":      "止损猎杀型",   # 插针猎杀止损单
    "UNKNOWN":        "信号混合",
}

# 做市商阶段
MM_PHASES = {
    "ACCUMULATION":   "建仓期",
    "MARKUP":         "拉盘期",
    "DISTRIBUTION":   "出货期",
    "WASHOUT":        "洗盘期",
    "STOP_HUNT_LONG": "猎杀多头",
    "STOP_HUNT_SHORT":"猎杀空头",
    "UNKNOWN":        "未知",
}


# ============================================================
# 主分类函数
# ============================================================

def classify_behavior(agg: dict,
                       market_context: dict,
                       kline_analysis: dict = None,
                       snapshot_fn=None,
                       token: str = "") -> dict:
    """
    综合判断代币当前行为类型和做市商阶段

    返回：
    {
        "behavior_type":  "SQUEEZE",
        "behavior_label": "逼空型",
        "mm_phase":       "拉盘期",
        "mm_probability": 78,        # 做市商介入概率 0~100
        "confidence":     "high",    # high/medium/low
        "signals":        [...],     # 支持该判断的信号列表
        "description":    "...",     # 一句话描述
    }
    """
    signals        = []
    mm_probability = 0

    # ── 基础数据提取 ──
    fm          = agg.get("funding_mean", 0)
    imbalances  = agg.get("imbalances", {})
    max_imb     = max(imbalances.values(), default=0)
    ois         = agg.get("ois", {})
    shares      = agg.get("oi_shares", {})
    max_share   = max(shares.values(), default=0)
    fundings    = agg.get("fundings", {})
    spread      = agg.get("max_futures_spread", 0)
    basis       = agg.get("max_basis", 0)
    total_oi    = agg.get("total_oi", 0)
    spot_avg    = agg.get("spot_avg", 0)

    # 市场背景
    btc_corr    = market_context.get("btc_correlation", 0)
    btc_change  = market_context.get("btc_change_4h", 0)
    excess      = market_context.get("excess_return", 0)
    market_wide = market_context.get("market_wide_move", False)

    # K线信号
    ka = kline_analysis or {}
    obv_pos_div  = ka.get("obv_signals", {}).get("positive_divergence", False)
    obv_neg_div  = ka.get("obv_signals", {}).get("negative_divergence", False)
    obv_breakout = ka.get("obv_signals", {}).get("obv_breakout", False)
    cmf_current  = ka.get("cmf_current", 0)
    cmf_turning  = ka.get("cmf_turning", False)
    trend_dir    = ka.get("trend", {}).get("direction", "unknown")
    patterns     = ka.get("patterns", {})
    vol_analysis = ka.get("volume_analysis", {})

    # Taker 比率
    raw_binance = agg.get("raw", {}).get("binance", {}).get("futures", {})
    taker       = raw_binance.get("taker_ratio", {}) or {}
    taker_cur   = taker.get("current", 1.0)
    top_ls      = raw_binance.get("top_ls_ratio", {}) or {}
    global_ls   = raw_binance.get("global_ls", {}) or {}
    top_long    = top_ls.get("top_long", False)
    retail_long = global_ls.get("retail_long", True)
    ticker      = raw_binance.get("ticker_24h", {}) or {}
    vol_oi      = (ticker.get("quote_vol_24h", 0) /
                   total_oi if total_oi > 0 else 0)

    # ── 做市商介入概率计算 ──

    # 低流通（容易控盘）
    float_ratio = market_context.get("float_ratio", 1.0)
    if float_ratio < 0.20:
        mm_probability += 25
        signals.append("极低流通量，高度可控盘")
    elif float_ratio < 0.30:
        mm_probability += 15
        signals.append("低流通量，易控盘")

    # OI高度集中
    if max_share > 0.60:
        mm_probability += 20
        signals.append(f"OI极度集中（单所{max_share*100:.0f}%）")
    elif max_share > 0.45:
        mm_probability += 10
        signals.append(f"OI集中（单所{max_share*100:.0f}%）")

    # 成交量刷量迹象
    if vol_oi > 25:
        mm_probability += 15
        signals.append(f"成交量/OI={vol_oi:.0f}x，刷量迹象")
    elif vol_oi > 15:
        mm_probability += 8
        signals.append(f"成交量/OI={vol_oi:.0f}x，偏高")

    # 跨所价差（做市商在操纵特定交易所）
    if spread > 0.01:
        mm_probability += 15
        signals.append(f"跨所价差{spread*100:.1f}%，单所操纵")
    elif spread > 0.003:
        mm_probability += 8
        signals.append(f"跨所价差{spread*100:.1f}%")

    # 大户vs散户背离（做市商持多，散户持空）
    if top_long and not retail_long and fm < -0.0003:
        mm_probability += 15
        signals.append("大户多头+散户空头+负费率，经典做市商设置")
    elif top_long and not retail_long:
        mm_probability += 8
        signals.append("大户多头vs散户空头")

    mm_probability = min(mm_probability, 95)

    # ── 行为类型判断 ──

    behavior_type = _classify_type(
        btc_corr, btc_change, excess, market_wide,
        spread, basis, fm, fundings,
        max_imb, max_share, taker_cur,
        top_long, retail_long,
        obv_pos_div, obv_neg_div, obv_breakout,
        cmf_current, cmf_turning,
        trend_dir, patterns, vol_analysis,
        agg, snapshot_fn, token
    )

    # ── 做市商阶段判断 ──
    mm_phase = _classify_mm_phase(
        behavior_type, fm, taker_cur,
        obv_pos_div, obv_neg_div, cmf_current,
        trend_dir, max_imb, agg, snapshot_fn, token
    )

    # ── 置信度 ──
    confidence = _calc_confidence(behavior_type, signals, mm_probability)

    # ── 描述生成 ──
    description = _generate_description(behavior_type, mm_phase, signals)

    return {
        "behavior_type":  behavior_type,
        "behavior_label": BEHAVIOR_TYPES.get(behavior_type, "未知"),
        "mm_phase":       MM_PHASES.get(mm_phase, "未知"),
        "mm_phase_key":   mm_phase,
        "mm_probability": mm_probability,
        "confidence":     confidence,
        "signals":        signals,
        "description":    description,
        "float_ratio":    float_ratio,
    }


# ============================================================
# 行为类型分类逻辑
# ============================================================

def _classify_type(
    btc_corr, btc_change, excess, market_wide,
    spread, basis, fm, fundings,
    max_imb, max_share, taker_cur,
    top_long, retail_long,
    obv_pos_div, obv_neg_div, obv_breakout,
    cmf_current, cmf_turning,
    trend_dir, patterns, vol_analysis,
    agg, snapshot_fn, token
) -> str:

    # ── 被动跟随（最先判断）──
    # 如果高度跟随BTC且没有主动信号，直接归为被动
    if btc_corr > 0.85 and market_wide and abs(excess) < 0.02:
        return "REACTIVE"

    # ── 出货型 ──
    # OI和价格同步下降，最明确的出货信号
    oi_falling    = _check_oi_falling(agg, snapshot_fn, token)
    price_falling = trend_dir == "downtrend"

    if oi_falling and price_falling and obv_neg_div:
        return "DISTRIBUTION"

    if oi_falling and price_falling and cmf_current < -0.1:
        return "DISTRIBUTION"

    # 双顶确认
    dt = patterns.get("double_top")
    if dt and dt.get("confirmed") and obv_neg_div:
        return "DISTRIBUTION"

    # ── 逼空型 ──
    # 资金费率持续为负+大户多头+OI上升
    neg_funding_count = sum(1 for f in fundings.values() if f < -0.0003)
    oi_rising = _check_oi_rising(agg, snapshot_fn, token)

    if (neg_funding_count >= 3
            and top_long and not retail_long
            and oi_rising):
        return "SQUEEZE"

    if (fm < -0.0005 and spread > 0.003 and oi_rising):
        return "SQUEEZE"

    # ── 拉高出货型 ──
    # 跨所价差大+OI暴增但价格在高位+出货信号
    if (spread > 0.005
            and basis > 0.02
            and obv_neg_div):
        return "PUMP_DUMP"

    if (spread > 0.01 and taker_cur < 0.9):
        return "PUMP_DUMP"

    # ── 止损猎杀型 ──
    wh = patterns.get("wick_hunt") if patterns else None
    if wh and wh.get("confirmed"):
        return "STOP_HUNT"

    # ── 借势拉盘型 ──
    # BTC在涨，该代币超额涨幅明显
    if (btc_change > 0.02
            and excess > 0.03
            and obv_breakout):
        return "MOMENTUM_RIDE"

    # ── TWAP建仓型 ──
    # 价格横盘+OBV上升+CMF转正+卖深变薄
    price_flat = abs(excess) < 0.02 and not market_wide
    if (price_flat
            and (obv_pos_div or obv_breakout)
            and (cmf_turning or cmf_current > 0.05)
            and max_imb > 0.3):
        return "TWAP_ACCUM"

    # ── 隐秘建仓型 ──
    # 更早期的建仓信号，价格还没有明显变化
    vol_ok = vol_analysis.get("vol_price_match", False)
    if (price_flat
            and obv_breakout
            and cmf_current > 0
            and not obv_neg_div):
        return "STEALTH_ACCUM"

    # ── 洗盘型 ──
    # 价格剧烈波动但OI稳定，量能高
    vol_high = vol_analysis.get("vol_ratio", 0) > 1.5
    if (vol_high
            and not oi_rising and not oi_falling
            and abs(excess) > 0.05):
        return "WASHOUT"

    return "UNKNOWN"


# ============================================================
# 做市商阶段判断
# ============================================================

def _classify_mm_phase(
    behavior_type, fm, taker_cur,
    obv_pos_div, obv_neg_div, cmf_current,
    trend_dir, max_imb,
    agg, snapshot_fn, token
) -> str:

    if behavior_type == "DISTRIBUTION":
        return "DISTRIBUTION"

    if behavior_type in ["TWAP_ACCUM", "STEALTH_ACCUM"]:
        return "ACCUMULATION"

    if behavior_type == "SQUEEZE":
        # 逼空进行中 = 拉盘期
        if taker_cur > 1.5 and fm < -0.0003:
            return "MARKUP"
        # 逼空蓄力 = 建仓期
        return "ACCUMULATION"

    if behavior_type == "PUMP_DUMP":
        # 价格在高位+出货信号 = 出货期
        if obv_neg_div or cmf_current < -0.05:
            return "DISTRIBUTION"
        return "MARKUP"

    if behavior_type == "WASHOUT":
        return "WASHOUT"

    if behavior_type == "STOP_HUNT":
        # 判断猎杀方向
        if taker_cur > 1.2:
            return "STOP_HUNT_SHORT"   # 打下去清空头
        return "STOP_HUNT_LONG"        # 打上去清多头

    if behavior_type == "MOMENTUM_RIDE":
        return "MARKUP"

    return "UNKNOWN"


# ============================================================
# 置信度计算
# ============================================================

def _calc_confidence(behavior_type: str,
                      signals: list,
                      mm_probability: int) -> str:
    if behavior_type == "UNKNOWN":
        return "low"

    score = len(signals) + (mm_probability // 20)

    if   score >= 6:  return "high"
    elif score >= 3:  return "medium"
    else:             return "low"


# ============================================================
# 描述生成
# ============================================================

def _generate_description(behavior_type: str,
                            mm_phase: str,
                            signals: list) -> str:
    desc_map = {
        "REACTIVE":      "跟随市场整体行情，非独立信号，建议观望",
        "SQUEEZE":       "做市商设置逼空陷阱，散户空头面临强制平仓风险",
        "PUMP_DUMP":     "快速拉升后出货，追高风险极高",
        "TWAP_ACCUM":    "做市商正在隐秘建仓，价格尚未启动，早期布局机会",
        "STEALTH_ACCUM": "发现隐秘资金流入迹象，仍在早期阶段，持续观察",
        "DISTRIBUTION":  "做市商正在出货，适合做空，避免做多",
        "WASHOUT":       "震仓洗盘，清洗浮筹，洗完可能继续上涨",
        "STOP_HUNT":     "做市商在猎杀止损单，插针后可能快速反转",
        "MOMENTUM_RIDE": "借BTC上涨势能拉盘，时效性强，注意追高风险",
        "UNKNOWN":       "信号混合，建议观望等待更明确的方向",
    }
    return desc_map.get(behavior_type, "信号混合")


# ============================================================
# 辅助函数
# ============================================================

def _check_oi_rising(agg: dict, snapshot_fn, token: str) -> bool:
    if not snapshot_fn:
        return False
    snaps = snapshot_fn(token, "binance", limit=5)
    if len(snaps) < 2:
        return False
    old = snaps[-1].get("total_oi", 0)
    new = snaps[0].get("total_oi", 0)
    return new > old * 1.05 if old > 0 else False


def _check_oi_falling(agg: dict, snapshot_fn, token: str) -> bool:
    if not snapshot_fn:
        return False
    snaps = snapshot_fn(token, "binance", limit=5)
    if len(snaps) < 2:
        return False
    old = snaps[-1].get("total_oi", 0)
    new = snaps[0].get("total_oi", 0)
    return new < old * 0.95 if old > 0 else False


# ============================================================
# 格式化输出（供推送使用）
# ============================================================

def format_behavior_tag(classification: dict) -> str:
    """生成推送里的行为标签"""
    btype  = classification.get("behavior_type", "UNKNOWN")
    blabel = classification.get("behavior_label", "信号混合")
    phase  = classification.get("mm_phase", "未知")
    mm_prob= classification.get("mm_probability", 0)
    conf   = classification.get("confidence", "low")

    conf_icon = "🔴" if conf == "high" else "🟡" if conf == "medium" else "⚪"

    icon_map = {
        "REACTIVE":      "〰️",
        "SQUEEZE":       "🔴",
        "PUMP_DUMP":     "⚠️",
        "TWAP_ACCUM":    "🔵",
        "STEALTH_ACCUM": "🔵",
        "DISTRIBUTION":  "🔴",
        "WASHOUT":       "🟡",
        "STOP_HUNT":     "⚡",
        "MOMENTUM_RIDE": "🟠",
        "UNKNOWN":       "⚫",
    }

    icon = icon_map.get(btype, "⚫")
    return f"{icon} {blabel} · {phase} · 做市商介入{mm_prob}% {conf_icon}"
