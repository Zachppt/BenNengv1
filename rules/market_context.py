# ============================================================
# rules/market_context.py — 市场背景分析 v4
# BTC相关性、超额涨幅、市场整体状态
# ============================================================

import statistics
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
# 市场背景分析主函数
# ============================================================

async def get_market_context(session,
                              token: str,
                              token_change_4h: float,
                              snapshot_fn,
                              all_results: list = None) -> dict:
    """
    计算市场背景数据：
    - BTC 4h涨幅
    - 该代币相对BTC的超额涨幅
    - BTC相关性（基于历史快照）
    - 市场是否整体在动
    - 流通量比例（从缓存或默认值）
    """

    # BTC 4h 涨幅
    btc_change = _get_btc_change(snapshot_fn)

    # 超额涨幅（相对BTC）
    excess_return = token_change_4h - btc_change

    # BTC相关性
    btc_correlation = _calc_btc_correlation(token, snapshot_fn)

    # 市场是否整体在动（超过40个代币同时涨>3%）
    market_wide_move = _check_market_wide(all_results or [])

    # 市场情绪
    sentiment = _calc_market_sentiment(btc_change, all_results or [])

    # 流通量（默认0.5，等待接入CoinGecko）
    float_ratio = _get_float_ratio(token)

    return {
        "btc_change_4h":    btc_change,
        "token_change_4h":  token_change_4h,
        "excess_return":    excess_return,
        "btc_correlation":  btc_correlation,
        "market_wide_move": market_wide_move,
        "market_sentiment": sentiment,
        "float_ratio":      float_ratio,
        "is_btc_driven":    btc_correlation > 0.8 and abs(excess_return) < 0.02,
    }


# ============================================================
# BTC 涨幅计算
# ============================================================

def _get_btc_change(snapshot_fn) -> float:
    """从BTC快照计算4h涨幅"""
    if not snapshot_fn:
        return 0.0
    try:
        snaps = snapshot_fn("BTC", "binance", limit=17)
        if len(snaps) >= 16:
            old = snaps[-1].get("price", 0)
            new = snaps[0].get("price", 0)
            if old > 0 and new > 0:
                return (new - old) / old
    except Exception:
        pass
    return 0.0


# ============================================================
# BTC 相关性计算
# ============================================================

def _calc_btc_correlation(token: str, snapshot_fn) -> float:
    """
    计算代币价格与BTC价格的相关性
    基于最近24条快照（约6小时）
    """
    if not snapshot_fn:
        return 0.0

    try:
        token_snaps = snapshot_fn(token, "binance", limit=24)
        btc_snaps   = snapshot_fn("BTC",  "binance", limit=24)

        if len(token_snaps) < 6 or len(btc_snaps) < 6:
            return 0.0

        # 对齐长度
        n = min(len(token_snaps), len(btc_snaps))
        token_prices = [s.get("price", 0) for s in token_snaps[:n]]
        btc_prices   = [s.get("price", 0) for s in btc_snaps[:n]]

        # 过滤零值
        pairs = [(t, b) for t, b in zip(token_prices, btc_prices)
                 if t > 0 and b > 0]
        if len(pairs) < 6:
            return 0.0

        token_vals = [p[0] for p in pairs]
        btc_vals   = [p[1] for p in pairs]

        # 皮尔逊相关系数
        n = len(pairs)
        mean_t = statistics.mean(token_vals)
        mean_b = statistics.mean(btc_vals)

        num = sum((t - mean_t) * (b - mean_b)
                  for t, b in zip(token_vals, btc_vals))
        den_t = sum((t - mean_t) ** 2 for t in token_vals) ** 0.5
        den_b = sum((b - mean_b) ** 2 for b in btc_vals) ** 0.5

        if den_t == 0 or den_b == 0:
            return 0.0

        corr = num / (den_t * den_b)
        return max(-1.0, min(1.0, corr))

    except Exception as e:
        logger.debug(f"BTC相关性计算失败 {token}: {e}")
        return 0.0


# ============================================================
# 市场整体动向检测
# ============================================================

def _check_market_wide(all_results: list,
                        threshold: int = 30) -> bool:
    """
    如果超过30个代币同时触发相同规则
    → 市场整体在动，不是单币信号
    """
    if not all_results:
        return False

    from collections import Counter
    rule_counts = Counter(
        r["rule"]
        for res in all_results
        for r in res.get("triggered", [])
    )
    max_count = max(rule_counts.values()) if rule_counts else 0
    return max_count > threshold


# ============================================================
# 市场情绪判断
# ============================================================

def _calc_market_sentiment(btc_change: float,
                             all_results: list) -> str:
    """
    综合判断市场整体情绪
    """
    if btc_change > 0.05:
        return "risk_on_strong"    # BTC大涨
    elif btc_change > 0.02:
        return "risk_on"           # BTC小涨
    elif btc_change < -0.05:
        return "risk_off_strong"   # BTC大跌
    elif btc_change < -0.02:
        return "risk_off"          # BTC小跌
    else:
        return "neutral"           # 横盘


# ============================================================
# 流通量获取（简化版，后续接入CoinGecko）
# ============================================================

# 已知低流通代币的流通率（手动维护，后续用API替代）
KNOWN_FLOAT_RATIOS = {
    "TRIA":   0.196,
    "COAI":   0.249,
    "MYX":    0.282,
    "RECALL": 0.350,
}

def _get_float_ratio(token: str) -> float:
    """
    获取代币流通量比例
    当前版本：已知代币用手动数据，未知代币默认0.5
    后续版本：接入CoinGecko API
    """
    return KNOWN_FLOAT_RATIOS.get(token.upper(), 0.5)


# ============================================================
# 超额涨幅分析
# ============================================================

def analyze_excess_return(token_change: float,
                           btc_change: float,
                           eth_change: float = 0) -> dict:
    """
    分析代币相对大盘的超额涨幅
    """
    vs_btc = token_change - btc_change
    vs_eth = token_change - eth_change if eth_change else None

    # 超额涨幅的强度判断
    if   vs_btc > 0.10:
        strength = "extreme"
        desc     = f"超额涨幅极强（超BTC {vs_btc*100:.1f}%）"
    elif vs_btc > 0.05:
        strength = "strong"
        desc     = f"超额涨幅明显（超BTC {vs_btc*100:.1f}%）"
    elif vs_btc > 0.02:
        strength = "moderate"
        desc     = f"轻微超额涨幅（超BTC {vs_btc*100:.1f}%）"
    elif vs_btc < -0.05:
        strength = "underperform"
        desc     = f"明显跑输BTC（差{abs(vs_btc)*100:.1f}%）"
    else:
        strength = "inline"
        desc     = "与BTC涨幅基本一致"

    return {
        "vs_btc":   vs_btc,
        "vs_eth":   vs_eth,
        "strength": strength,
        "desc":     desc,
    }


# ============================================================
# 48小时OI趋势分析（解决慢启动漏报）
# ============================================================

def analyze_48h_oi_trend(token: str, snapshot_fn) -> dict:
    """
    分析过去48小时的OI趋势
    解决 RECALL 类"慢启动"代币的漏报问题
    """
    if not snapshot_fn:
        return {}

    try:
        # 48小时 = 192条15分钟快照
        snaps = snapshot_fn(token, "binance", limit=192)
        if len(snaps) < 16:
            return {"sufficient_data": False}

        oi_values = [s.get("total_oi", 0) for s in reversed(snaps)
                     if s.get("total_oi", 0) > 0]

        if len(oi_values) < 8:
            return {"sufficient_data": False}

        # 48h总变化
        oi_change_total = (oi_values[-1] - oi_values[0]) / oi_values[0]

        # 每4小时的变化率（检测是否是缓慢积累）
        chunk_size = max(1, len(oi_values) // 12)
        chunks     = [oi_values[i:i+chunk_size]
                      for i in range(0, len(oi_values), chunk_size)]
        chunk_avgs = [statistics.mean(c) for c in chunks if c]

        # 判断是否单调上升（缓慢积累）
        monotonic_rising = all(
            chunk_avgs[i] <= chunk_avgs[i+1]
            for i in range(len(chunk_avgs)-1)
        ) if len(chunk_avgs) > 1 else False

        # 每次变化幅度是否都很小（TWAP特征）
        chunk_changes = []
        for i in range(1, len(chunk_avgs)):
            if chunk_avgs[i-1] > 0:
                chunk_changes.append(
                    (chunk_avgs[i] - chunk_avgs[i-1]) / chunk_avgs[i-1]
                )

        max_single_change = max(abs(c) for c in chunk_changes) \
                           if chunk_changes else 0
        avg_change        = statistics.mean(abs(c) for c in chunk_changes) \
                           if chunk_changes else 0

        # 判断：48h总涨>15%，但每次涨幅<5%，且单调上升
        slow_accumulation = (
            oi_change_total > 0.15
            and max_single_change < 0.05
            and monotonic_rising
        )

        return {
            "sufficient_data":    True,
            "oi_change_48h":      oi_change_total,
            "monotonic_rising":   monotonic_rising,
            "max_single_change":  max_single_change,
            "avg_change":         avg_change,
            "slow_accumulation":  slow_accumulation,
            "current_oi":         oi_values[-1],
            "oi_48h_ago":         oi_values[0],
        }

    except Exception as e:
        logger.debug(f"48h OI分析失败 {token}: {e}")
        return {"sufficient_data": False}


# ============================================================
# 低波动横盘检测（RECALL类早期信号）
# ============================================================

def detect_sideways_accumulation(token: str,
                                  snapshot_fn,
                                  kline_analysis: dict) -> dict:
    """
    检测低波动横盘+量能异常（TWAP建仓早期阶段）

    RECALL 类代币在拉升前会有：
    1. 价格横盘（4h波动<2%）
    2. 成交量比过去7天均值高
    3. OBV持续上升
    4. 卖方深度持续变薄
    """
    if not snapshot_fn or not kline_analysis:
        return {}

    try:
        snaps = snapshot_fn(token, "binance", limit=48)
        if len(snaps) < 16:
            return {"sufficient_data": False}

        prices  = [s.get("price", 0) for s in reversed(snaps) if s.get("price")]
        ask_depths = [s.get("ask_depth_usd", 0)
                      for s in reversed(snaps) if s.get("ask_depth_usd")]

        if not prices:
            return {"sufficient_data": False}

        # 价格波动率（最近4小时）
        recent_prices = prices[-16:] if len(prices) >= 16 else prices
        price_range   = (max(recent_prices) - min(recent_prices)) / \
                        statistics.mean(recent_prices) if recent_prices else 0

        # 卖方深度趋势
        ask_trend_falling = False
        if len(ask_depths) >= 8:
            recent_ask = ask_depths[-8:]
            ask_trend_falling = (recent_ask[-1] < recent_ask[0] * 0.90)

        # OBV信号
        obv_rising = kline_analysis.get("obv_signals", {}).get("obv_breakout", False) or \
                     kline_analysis.get("obv_signals", {}).get("positive_divergence", False)

        # CMF
        cmf = kline_analysis.get("cmf_current", 0)
        cmf_positive = cmf > 0.05

        # 综合判断
        is_sideways_accum = (
            price_range < 0.03          # 价格横盘（<3%波动）
            and ask_trend_falling       # 卖方深度在变薄
            and (obv_rising or cmf_positive)  # 资金在流入
        )

        return {
            "sufficient_data":    True,
            "price_range":        price_range,
            "ask_trend_falling":  ask_trend_falling,
            "obv_rising":         obv_rising,
            "cmf_positive":       cmf_positive,
            "is_sideways_accum":  is_sideways_accum,
        }

    except Exception as e:
        logger.debug(f"横盘检测失败 {token}: {e}")
        return {"sufficient_data": False}
