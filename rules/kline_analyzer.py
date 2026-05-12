# ============================================================
# rules/kline_analyzer.py — K线结构分析模块 v3
# 指标：OBV、VPVR、CMF、成交量结构
# 形态：双顶、双底、下行通道突破、旗形
# ============================================================

import statistics
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
# 基础指标计算
# ============================================================

def calc_obv(klines: list) -> list:
    """
    OBV（能量潮）计算
    klines: 时间正序列表，每项含 open/high/low/close/volume
    返回：OBV序列（与klines等长）
    """
    if not klines:
        return []
    obv = [0.0]
    for i in range(1, len(klines)):
        prev_close = klines[i-1]["close"]
        curr_close = klines[i]["close"]
        vol        = klines[i]["volume"]
        if curr_close > prev_close:
            obv.append(obv[-1] + vol)
        elif curr_close < prev_close:
            obv.append(obv[-1] - vol)
        else:
            obv.append(obv[-1])
    return obv


def calc_cmf(klines: list, period: int = 21) -> list:
    """
    CMF（资金流量指标）计算
    衡量资金流入/流出强度
    范围：-1 到 +1
    """
    if len(klines) < period:
        return [0.0] * len(klines)

    cmf_values = []
    for i in range(len(klines)):
        if i < period - 1:
            cmf_values.append(0.0)
            continue

        window = klines[i - period + 1: i + 1]
        mfv_sum = 0.0
        vol_sum = 0.0

        for k in window:
            h = k["high"]
            l = k["low"]
            c = k["close"]
            v = k["volume"]
            if h == l:
                mfm = 0.0
            else:
                mfm = ((c - l) - (h - c)) / (h - l)
            mfv_sum += mfm * v
            vol_sum += v

        cmf_values.append(mfv_sum / vol_sum if vol_sum > 0 else 0.0)

    return cmf_values


def calc_vpvr(klines: list, bins: int = 20) -> dict:
    """
    VPVR（成交量分布）简化版
    把价格范围分成N个区间，统计每个区间的累计成交量
    返回：{
        "poc": POC价格（最大成交量价格）,
        "vah": 价值区间上沿（70%成交量覆盖的上边界）,
        "val": 价值区间下沿,
        "distribution": [(price_mid, volume), ...]
    }
    """
    if not klines:
        return {"poc": 0, "vah": 0, "val": 0, "distribution": []}

    all_highs  = [k["high"]  for k in klines]
    all_lows   = [k["low"]   for k in klines]
    price_high = max(all_highs)
    price_low  = min(all_lows)

    if price_high == price_low:
        return {"poc": price_high, "vah": price_high,
                "val": price_low, "distribution": []}

    bin_size   = (price_high - price_low) / bins
    vol_by_bin = [0.0] * bins

    for k in klines:
        # 把每根K线的成交量按价格区间分配
        k_high = k["high"]
        k_low  = k["low"]
        k_vol  = k["volume"]
        k_range = k_high - k_low if k_high > k_low else bin_size

        for b in range(bins):
            bin_low  = price_low + b * bin_size
            bin_high = bin_low + bin_size
            # 计算K线与该区间的重叠比例
            overlap_low  = max(k_low, bin_low)
            overlap_high = min(k_high, bin_high)
            if overlap_high > overlap_low:
                ratio = (overlap_high - overlap_low) / k_range
                vol_by_bin[b] += k_vol * ratio

    # POC：成交量最大的区间
    poc_bin   = vol_by_bin.index(max(vol_by_bin))
    poc_price = price_low + (poc_bin + 0.5) * bin_size

    # VAH/VAL：覆盖70%成交量的价格区间
    total_vol = sum(vol_by_bin)
    target    = total_vol * 0.70
    # 从POC向两侧扩展
    val_bin   = poc_bin
    vah_bin   = poc_bin
    covered   = vol_by_bin[poc_bin]
    left      = poc_bin - 1
    right     = poc_bin + 1

    while covered < target:
        left_vol  = vol_by_bin[left]  if left  >= 0    else 0
        right_vol = vol_by_bin[right] if right < bins  else 0
        if left_vol >= right_vol and left >= 0:
            covered += left_vol
            val_bin  = left
            left    -= 1
        elif right < bins:
            covered += right_vol
            vah_bin  = right
            right   += 1
        else:
            break

    vah = price_low + (vah_bin + 1) * bin_size
    val = price_low + val_bin * bin_size

    distribution = [
        (price_low + (b + 0.5) * bin_size, vol_by_bin[b])
        for b in range(bins)
    ]

    return {
        "poc":          poc_price,
        "vah":          vah,
        "val":          val,
        "distribution": distribution,
        "price_high":   price_high,
        "price_low":    price_low,
    }


def calc_macd(klines: list,
              fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """
    MACD计算（辅助指标）
    返回最新的 macd线、信号线、柱状图
    """
    if len(klines) < slow + signal:
        return {"macd": 0, "signal": 0, "histogram": 0, "cross": None}

    closes = [k["close"] for k in klines]

    def ema(data, period):
        k = 2 / (period + 1)
        result = [data[0]]
        for i in range(1, len(data)):
            result.append(data[i] * k + result[-1] * (1 - k))
        return result

    ema_fast   = ema(closes, fast)
    ema_slow   = ema(closes, slow)
    macd_line  = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line= ema(macd_line[slow-1:], signal)

    # 对齐长度
    offset     = slow - 1
    macd_cur   = macd_line[-1]
    sig_cur    = signal_line[-1]
    hist_cur   = macd_cur - sig_cur

    macd_prev  = macd_line[-2]  if len(macd_line)  > 1 else macd_cur
    sig_prev   = signal_line[-2] if len(signal_line) > 1 else sig_cur

    # 金叉/死叉
    cross = None
    if macd_prev < sig_prev and macd_cur >= sig_cur:
        cross = "golden"
    elif macd_prev > sig_prev and macd_cur <= sig_cur:
        cross = "death"

    return {
        "macd":      macd_cur,
        "signal":    sig_cur,
        "histogram": hist_cur,
        "cross":     cross,
        "above_zero": macd_cur > 0,
    }


# ============================================================
# 形态识别
# ============================================================

def detect_double_bottom(klines: list,
                          tolerance: float = 0.02) -> Optional[dict]:
    """
    双底（W底）识别
    条件：
    1. 两个相近低点（差距 < tolerance）
    2. 两低点之间有明显反弹（> 5%）
    3. 第二个底成交量 ≤ 第一个底（恐慌减弱）
    4. OBV在第二个底出现正背离
    """
    if len(klines) < 10:
        return None

    obv = calc_obv(klines)
    lows = [(i, klines[i]["low"], klines[i]["volume"])
            for i in range(len(klines))]

    # 找局部低点（前后各2根K线都高于它）
    local_lows = []
    for i in range(2, len(lows) - 2):
        if (lows[i][1] < lows[i-1][1] and lows[i][1] < lows[i-2][1]
                and lows[i][1] < lows[i+1][1] and lows[i][1] < lows[i+2][1]):
            local_lows.append(lows[i])

    if len(local_lows) < 2:
        return None

    # 检查最近两个低点
    for j in range(len(local_lows) - 1):
        b1_idx, b1_price, b1_vol = local_lows[j]
        b2_idx, b2_price, b2_vol = local_lows[j + 1]

        # 条件1：价格相近
        price_diff = abs(b1_price - b2_price) / b1_price
        if price_diff > tolerance:
            continue

        # 条件2：中间有反弹
        between = klines[b1_idx:b2_idx]
        if not between:
            continue
        peak = max(k["high"] for k in between)
        rebound = (peak - b1_price) / b1_price
        if rebound < 0.05:
            continue

        # 条件3：第二个底成交量 ≤ 第一个底
        vol_shrink = b2_vol <= b1_vol * 1.1

        # 条件4：OBV正背离
        obv_b1 = obv[b1_idx]
        obv_b2 = obv[b2_idx]
        obv_divergence = b2_price <= b1_price and obv_b2 > obv_b1

        if obv_divergence:
            neckline = peak
            return {
                "pattern":        "double_bottom",
                "bottom1_price":  b1_price,
                "bottom2_price":  b2_price,
                "neckline":       neckline,
                "vol_shrink":     vol_shrink,
                "obv_divergence": True,
                "confirmed":      klines[-1]["close"] > neckline,
                "strength":       "strong" if vol_shrink else "moderate",
            }

    return None


def detect_double_top(klines: list,
                       tolerance: float = 0.02) -> Optional[dict]:
    """
    双顶（M头）识别
    条件：
    1. 两个相近高点
    2. 第二个顶成交量 < 第一个顶（量能萎缩）
    3. OBV顶背离
    """
    if len(klines) < 10:
        return None

    obv = calc_obv(klines)
    highs = [(i, klines[i]["high"], klines[i]["volume"])
             for i in range(len(klines))]

    local_highs = []
    for i in range(2, len(highs) - 2):
        if (highs[i][1] > highs[i-1][1] and highs[i][1] > highs[i-2][1]
                and highs[i][1] > highs[i+1][1] and highs[i][1] > highs[i+2][1]):
            local_highs.append(highs[i])

    if len(local_highs) < 2:
        return None

    for j in range(len(local_highs) - 1):
        t1_idx, t1_price, t1_vol = local_highs[j]
        t2_idx, t2_price, t2_vol = local_highs[j + 1]

        price_diff = abs(t1_price - t2_price) / t1_price
        if price_diff > tolerance:
            continue

        # 量能萎缩
        vol_shrink = t2_vol < t1_vol * 0.9

        # OBV顶背离
        obv_t1 = obv[t1_idx]
        obv_t2 = obv[t2_idx]
        obv_divergence = t2_price >= t1_price * 0.99 and obv_t2 < obv_t1

        if obv_divergence or vol_shrink:
            between    = klines[t1_idx:t2_idx]
            neckline   = min(k["low"] for k in between) if between else t1_price * 0.95
            return {
                "pattern":        "double_top",
                "top1_price":     t1_price,
                "top2_price":     t2_price,
                "neckline":       neckline,
                "vol_shrink":     vol_shrink,
                "obv_divergence": obv_divergence,
                "confirmed":      klines[-1]["close"] < neckline,
                "strength":       "strong" if (vol_shrink and obv_divergence)
                                  else "moderate",
            }

    return None


def detect_channel_breakout(klines: list,
                              min_touches: int = 4) -> Optional[dict]:
    """
    下行通道突破识别
    条件：
    1. 价格在下降通道内运行（至少4个接触点）
    2. 突破上轨用收盘价（不是影线）
    3. 突破K线成交量 > 过去10根均量的1.5倍
    4. OBV同步突破
    """
    if len(klines) < min_touches + 3:
        return None

    obv = calc_obv(klines)

    # 用线性回归找通道上轨和下轨
    n = len(klines)
    x = list(range(n))
    highs  = [k["high"]  for k in klines]
    lows   = [k["low"]   for k in klines]
    closes = [k["close"] for k in klines]

    # 简化：用最近N根K线的高点和低点线性回归
    def linear_regression(y_vals):
        n = len(y_vals)
        x_mean = (n - 1) / 2
        y_mean = sum(y_vals) / n
        num = sum((i - x_mean) * (y - y_mean)
                  for i, y in enumerate(y_vals))
        den = sum((i - x_mean) ** 2 for i in range(n))
        slope = num / den if den != 0 else 0
        intercept = y_mean - slope * x_mean
        return slope, intercept

    high_slope, high_intercept = linear_regression(highs)
    low_slope,  low_intercept  = linear_regression(lows)

    # 通道必须是下降的
    if high_slope >= 0:
        return None

    # 计算当前上轨价格
    upper_now = high_slope * (n - 1) + high_intercept
    lower_now = low_slope  * (n - 1) + low_intercept

    current_close = closes[-1]
    current_high  = highs[-1]

    # 检查是否突破上轨（用收盘价）
    if current_close <= upper_now:
        return None

    # 成交量确认
    recent_vols = [k["volume"] for k in klines[-11:-1]]
    avg_vol     = statistics.mean(recent_vols) if recent_vols else 0
    curr_vol    = klines[-1]["volume"]
    vol_surge   = curr_vol > avg_vol * 1.5 if avg_vol > 0 else False

    # OBV突破：当前OBV是否超过过去20根的最高点
    obv_lookback = obv[-21:-1] if len(obv) > 21 else obv[:-1]
    obv_max_prev = max(obv_lookback) if obv_lookback else 0
    obv_breakout = obv[-1] > obv_max_prev

    # 统计通道接触点
    touch_count = sum(
        1 for i in range(n - 3)
        if abs(highs[i] - (high_slope * i + high_intercept)) / highs[i] < 0.015
    )

    confirmed = vol_surge and obv_breakout

    return {
        "pattern":      "channel_breakout",
        "channel_slope":high_slope,
        "upper_now":    upper_now,
        "lower_now":    lower_now,
        "touch_count":  touch_count,
        "vol_surge":    vol_surge,
        "vol_ratio":    curr_vol / avg_vol if avg_vol > 0 else 0,
        "obv_breakout": obv_breakout,
        "confirmed":    confirmed,
        "strength":     "strong" if confirmed else "unconfirmed",
    }


def detect_flag_breakout(klines: list) -> Optional[dict]:
    """
    旗形突破识别
    条件：
    1. 前段急速拉升（旗杆）：5根K线内涨幅>15%
    2. 后段横盘整理（旗面）：量能萎缩
    3. 突破旗形上沿+量能放大
    """
    if len(klines) < 15:
        return None

    closes  = [k["close"]  for k in klines]
    volumes = [k["volume"] for k in klines]

    # 找旗杆：过去15根K线内是否有急速拉升
    flagpole_end = None
    for i in range(5, min(15, len(klines))):
        idx  = len(klines) - 1 - i
        gain = (closes[idx + 5] - closes[idx]) / closes[idx]
        if gain > 0.15:
            flagpole_end = idx + 5
            flagpole_gain = gain
            break

    if flagpole_end is None:
        return None

    # 旗面：旗杆后到现在
    flag_klines  = klines[flagpole_end:]
    if len(flag_klines) < 3:
        return None

    flag_highs   = [k["high"]   for k in flag_klines]
    flag_volumes = [k["volume"] for k in flag_klines]
    flag_top     = max(flag_highs)

    # 量能萎缩
    pole_avg_vol = statistics.mean(volumes[max(0, flagpole_end-5):flagpole_end])
    flag_avg_vol = statistics.mean(flag_volumes)
    vol_shrink   = flag_avg_vol < pole_avg_vol * 0.7

    # 突破旗面上沿
    curr_close   = closes[-1]
    curr_vol     = volumes[-1]
    breakout     = curr_close > flag_top
    vol_surge    = curr_vol > flag_avg_vol * 1.5

    if breakout and vol_surge:
        return {
            "pattern":      "flag_breakout",
            "flagpole_gain":flagpole_gain,
            "flag_top":     flag_top,
            "vol_shrink":   vol_shrink,
            "vol_surge":    vol_surge,
            "confirmed":    True,
            "strength":     "strong" if vol_shrink else "moderate",
        }

    return None


# ============================================================
# OBV 信号检测
# ============================================================

def detect_obv_signals(klines: list, obv: list) -> dict:
    """
    检测OBV的关键信号
    """
    if len(klines) < 5 or len(obv) < 5:
        return {}

    closes    = [k["close"] for k in klines]
    n         = len(obv)
    lookback  = min(20, n - 1)

    # OBV突破前高
    obv_max_prev = max(obv[-lookback-1:-1]) if lookback > 0 else obv[0]
    obv_breakout = obv[-1] > obv_max_prev

    # OBV底背离：价格创新低但OBV没有创新低
    price_min_prev = min(closes[-lookback-1:-1]) if lookback > 0 else closes[0]
    obv_min_prev   = min(obv[-lookback-1:-1])    if lookback > 0 else obv[0]
    pos_divergence = (closes[-1] <= price_min_prev * 1.02
                      and obv[-1] > obv_min_prev)

    # OBV顶背离：价格创新高但OBV没有创新高
    price_max_prev = max(closes[-lookback-1:-1]) if lookback > 0 else closes[0]
    obv_max_prev2  = max(obv[-lookback-1:-1])    if lookback > 0 else obv[0]
    neg_divergence = (closes[-1] >= price_max_prev * 0.98
                      and obv[-1] < obv_max_prev2)

    # OBV趋势方向
    obv_trend = "rising" if obv[-1] > obv[-5] else "falling"

    return {
        "obv_breakout":        obv_breakout,
        "positive_divergence": pos_divergence,
        "negative_divergence": neg_divergence,
        "obv_trend":           obv_trend,
        "obv_current":         obv[-1],
        "obv_max_prev":        obv_max_prev,
    }


# ============================================================
# 成交量结构分析
# ============================================================

def analyze_volume_structure(klines: list) -> dict:
    """
    分析成交量结构
    """
    if len(klines) < 5:
        return {}

    volumes = [k["volume"] for k in klines]
    closes  = [k["close"]  for k in klines]
    avg_vol = statistics.mean(volumes[-10:]) if len(volumes) >= 10 else statistics.mean(volumes)

    curr_vol   = volumes[-1]
    vol_ratio  = curr_vol / avg_vol if avg_vol > 0 else 1

    # 成交量趋势：过去5根是否递增
    recent_vols = volumes[-5:]
    vol_increasing = all(recent_vols[i] <= recent_vols[i+1]
                         for i in range(len(recent_vols)-1))

    # 量价配合：上涨时成交量是否放大
    up_vols   = [volumes[i] for i in range(1, len(klines))
                 if closes[i] > closes[i-1]]
    down_vols = [volumes[i] for i in range(1, len(klines))
                 if closes[i] < closes[i-1]]

    avg_up_vol   = statistics.mean(up_vols)   if up_vols   else 0
    avg_down_vol = statistics.mean(down_vols) if down_vols else 0
    vol_price_match = avg_up_vol > avg_down_vol  # 上涨时成交量更大

    # 放量突破判断
    recent_highs = [k["high"] for k in klines[-11:-1]]
    prev_high    = max(recent_highs) if recent_highs else 0
    breakout_vol = (closes[-1] > prev_high and vol_ratio > 1.5)

    return {
        "avg_volume":       avg_vol,
        "current_volume":   curr_vol,
        "vol_ratio":        vol_ratio,
        "vol_surge":        vol_ratio > 1.5,
        "vol_increasing":   vol_increasing,
        "vol_price_match":  vol_price_match,
        "breakout_with_vol":breakout_vol,
        "prev_high":        prev_high,
    }


# ============================================================
# 支撑阻力位识别
# ============================================================

def find_support_resistance(klines: list) -> dict:
    """
    基于成交量密集区找支撑阻力
    不用斐波那契，用VPVR和前高前低
    """
    if len(klines) < 5:
        return {}

    vpvr    = calc_vpvr(klines)
    highs   = [k["high"]  for k in klines]
    lows    = [k["low"]   for k in klines]
    closes  = [k["close"] for k in klines]

    recent_high = max(highs[-24:]) if len(highs) >= 24 else max(highs)
    recent_low  = min(lows[-24:])  if len(lows)  >= 24 else min(lows)
    curr_price  = closes[-1]

    # 关键支撑/阻力：POC、VAH、VAL、前高、前低
    levels = {
        "poc":         vpvr.get("poc", 0),
        "vah":         vpvr.get("vah", 0),
        "val":         vpvr.get("val", 0),
        "recent_high": recent_high,
        "recent_low":  recent_low,
    }

    # 当前价格相对POC的位置
    poc = vpvr.get("poc", 0)
    if poc > 0:
        above_poc   = curr_price > poc
        poc_distance= (curr_price - poc) / poc
    else:
        above_poc    = False
        poc_distance = 0

    return {
        "levels":       levels,
        "above_poc":    above_poc,
        "poc_distance": poc_distance,
        "curr_price":   curr_price,
        "vpvr":         vpvr,
    }


# ============================================================
# 主分析函数
# ============================================================

def analyze_klines(klines: list, oi_data: list = None) -> dict:
    """
    对K线数据进行完整的技术分析
    klines: 时间正序，每项含 open/high/low/close/volume/ts
    返回：完整的技术分析结果
    """
    if not klines or len(klines) < 5:
        return {"error": "K线数据不足"}

    # 基础指标
    obv    = calc_obv(klines)
    cmf    = calc_cmf(klines)
    macd   = calc_macd(klines)
    vpvr   = calc_vpvr(klines)
    sr     = find_support_resistance(klines)
    vol    = analyze_volume_structure(klines)
    obv_signals = detect_obv_signals(klines, obv)

    # CMF状态
    cmf_current = cmf[-1] if cmf else 0
    cmf_prev    = cmf[-5] if len(cmf) >= 5 else 0
    cmf_turning = cmf_prev < -0.05 and cmf_current > 0
    cmf_positive= cmf_current > 0.1

    # 形态识别
    double_bottom   = detect_double_bottom(klines)
    double_top      = detect_double_top(klines)
    channel_breakout= detect_channel_breakout(klines)
    flag_breakout   = detect_flag_breakout(klines)

    # 趋势判断
    closes = [k["close"] for k in klines]
    trend  = _detect_trend(klines)

    # 综合K线信号评分
    kline_signals  = []
    kline_score    = 0

    # OBV信号
    if obv_signals.get("obv_breakout"):
        kline_signals.append({
            "signal": "obv_breakout",
            "desc":   "OBV突破前高（资金提前流入）",
            "score":  10,
            "bullish":True,
        })
        kline_score += 10

    if obv_signals.get("positive_divergence"):
        kline_signals.append({
            "signal": "obv_divergence_pos",
            "desc":   "OBV底背离（价跌OBV未跌，看涨）",
            "score":  11,
            "bullish":True,
        })
        kline_score += 11

    if obv_signals.get("negative_divergence"):
        kline_signals.append({
            "signal": "obv_divergence_neg",
            "desc":   "OBV顶背离（价涨OBV未涨，看跌）",
            "score":  -8,
            "bullish":False,
        })
        kline_score -= 8

    # CMF信号
    if cmf_turning:
        kline_signals.append({
            "signal": "cmf_turning",
            "desc":   f"CMF从负转正（{cmf_prev:.3f}→{cmf_current:.3f}，机构建仓）",
            "score":  9,
            "bullish":True,
        })
        kline_score += 9

    elif cmf_positive:
        kline_signals.append({
            "signal": "cmf_positive",
            "desc":   f"CMF持续为正（{cmf_current:.3f}，资金持续流入）",
            "score":  6,
            "bullish":True,
        })
        kline_score += 6

    # VPVR信号
    if sr.get("above_poc"):
        kline_signals.append({
            "signal": "vpvr_above_poc",
            "desc":   f"价格突破成交量POC（${vpvr.get('poc',0):.5g}）",
            "score":  8,
            "bullish":True,
        })
        kline_score += 8

    # 成交量信号
    if vol.get("breakout_with_vol"):
        kline_signals.append({
            "signal": "volume_surge_breakout",
            "desc":   f"放量突破前高（量比{vol.get('vol_ratio',0):.1f}x）",
            "score":  8,
            "bullish":True,
        })
        kline_score += 8

    # 形态信号
    if channel_breakout and channel_breakout.get("confirmed"):
        kline_signals.append({
            "signal": "channel_breakout",
            "desc":   (f"下行通道突破确认"
                      f"（量比{channel_breakout.get('vol_ratio',0):.1f}x"
                      f"，OBV{'突破' if channel_breakout.get('obv_breakout') else '未突破'}）"),
            "score":  10,
            "bullish":True,
        })
        kline_score += 10

    if double_bottom and double_bottom.get("obv_divergence"):
        kline_signals.append({
            "signal": "double_bottom",
            "desc":   (f"双底形态（OBV正背离确认"
                      f"{'，量能萎缩' if double_bottom.get('vol_shrink') else ''}）"),
            "score":  9,
            "bullish":True,
        })
        kline_score += 9

    if double_top and double_top.get("obv_divergence"):
        kline_signals.append({
            "signal": "double_top",
            "desc":   "双顶形态（OBV负背离确认，看跌）",
            "score":  -6,
            "bullish":False,
        })
        kline_score -= 6

    if flag_breakout:
        kline_signals.append({
            "signal": "flag_breakout",
            "desc":   f"旗形突破（旗杆涨幅{flag_breakout.get('flagpole_gain',0)*100:.1f}%）",
            "score":  8,
            "bullish":True,
        })
        kline_score += 8

    # MACD信号（辅助）
    if macd.get("cross") == "golden" and macd.get("above_zero"):
        kline_signals.append({
            "signal": "macd_cross_above_zero",
            "desc":   "MACD零轴上方金叉（趋势确认）",
            "score":  5,
            "bullish":True,
        })
        kline_score += 5

    return {
        "kline_score":    kline_score,
        "kline_signals":  kline_signals,
        "trend":          trend,
        "obv_signals":    obv_signals,
        "cmf_current":    cmf_current,
        "cmf_turning":    cmf_turning,
        "macd":           macd,
        "vpvr":           vpvr,
        "support_resistance": sr,
        "volume_analysis":    vol,
        "patterns": {
            "double_bottom":    double_bottom,
            "double_top":       double_top,
            "channel_breakout": channel_breakout,
            "flag_breakout":    flag_breakout,
        },
        "obv_series": obv[-10:],   # 最近10个值
        "cmf_series": cmf[-10:],
    }


def _detect_trend(klines: list) -> dict:
    """
    趋势判断：Higher High + Higher Low = 上升趋势
    """
    if len(klines) < 6:
        return {"direction": "unknown", "desc": "数据不足"}

    highs  = [k["high"]  for k in klines[-12:]]
    lows   = [k["low"]   for k in klines[-12:]]
    closes = [k["close"] for k in klines[-12:]]

    # 简化：用线性回归判断趋势方向
    n = len(closes)
    x_mean = (n - 1) / 2
    y_mean = sum(closes) / n
    num    = sum((i - x_mean) * (c - y_mean) for i, c in enumerate(closes))
    den    = sum((i - x_mean) ** 2 for i in range(n))
    slope  = num / den if den != 0 else 0

    # 上升/下降幅度
    slope_pct = slope / closes[0] if closes[0] > 0 else 0

    if   slope_pct >  0.005:
        direction = "uptrend"
        desc      = "上升趋势"
    elif slope_pct < -0.005:
        direction = "downtrend"
        desc      = "下降趋势"
    else:
        direction = "sideways"
        desc      = "横盘震荡"

    return {
        "direction":  direction,
        "desc":       desc,
        "slope_pct":  slope_pct,
        "curr_price": closes[-1],
        "range_high": max(highs),
        "range_low":  min(lows),
    }


# ============================================================
# 文字描述生成（供推送使用）
# ============================================================

def format_kline_summary(analysis: dict) -> str:
    """
    把K线分析结果转成Telegram推送的文字描述
    """
    if not analysis or analysis.get("error"):
        return "K线数据不足，无法分析"

    lines = []

    # 趋势
    trend = analysis.get("trend", {})
    lines.append(f"趋势：{trend.get('desc', '未知')}")

    # 形态
    patterns = analysis.get("patterns", {})
    cb = patterns.get("channel_breakout")
    db = patterns.get("double_bottom")
    dt = patterns.get("double_top")
    fb = patterns.get("flag_breakout")

    if cb and cb.get("confirmed"):
        lines.append(
            f"形态：下行通道突破✅"
            f"（量比{cb.get('vol_ratio',0):.1f}x"
            f"，OBV{'突破' if cb.get('obv_breakout') else '待确认'}）"
        )
    elif db and db.get("obv_divergence"):
        status = "已确认" if db.get("confirmed") else "颈线待突破"
        lines.append(f"形态：双底W底（{status}，OBV正背离✅）")
    elif dt and dt.get("obv_divergence"):
        lines.append("形态：双顶M头（OBV负背离，看跌⚠️）")
    elif fb:
        lines.append(
            f"形态：旗形突破✅"
            f"（旗杆+{fb.get('flagpole_gain',0)*100:.1f}%）"
        )

    # 关键指标
    cmf = analysis.get("cmf_current", 0)
    cmf_status = "资金流入" if cmf > 0.05 else "资金流出" if cmf < -0.05 else "中性"
    lines.append(f"CMF：{cmf:.3f}（{cmf_status}）")

    obv_sig = analysis.get("obv_signals", {})
    if obv_sig.get("obv_breakout"):
        lines.append("OBV：突破前高（资金提前布局✅）")
    elif obv_sig.get("positive_divergence"):
        lines.append("OBV：底背离（看涨信号✅）")
    elif obv_sig.get("negative_divergence"):
        lines.append("OBV：顶背离（看跌信号⚠️）")

    # 成交量
    vol = analysis.get("volume_analysis", {})
    if vol.get("breakout_with_vol"):
        lines.append(f"成交量：放量突破前高（量比{vol.get('vol_ratio',0):.1f}x✅）")
    elif vol.get("vol_surge"):
        lines.append(f"成交量：异常放量（量比{vol.get('vol_ratio',0):.1f}x）")

    # 支撑阻力
    sr  = analysis.get("support_resistance", {})
    poc = sr.get("levels", {}).get("poc", 0)
    if poc > 0:
        lines.append(f"关键支撑（POC）：${poc:.5g}")

    return "\n".join(lines)
