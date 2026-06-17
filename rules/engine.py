# ============================================================
# rules/engine.py — v4
# 双向概率系统（做多 + 做空）
# 整合行为分类器
# ============================================================

import time
import logging
import statistics
from typing import Optional
from config import (
    SIGNAL_WEIGHTS, BASE_PROBABILITY,
    MAX_SIGNAL_SCORE, PROB_CAP, PROB_FLOOR,
    BASIS_THRESHOLD
)
from rules.kline_analyzer import analyze_klines, format_kline_summary
from rules.behavior_classifier import classify_behavior, format_behavior_tag
from rules.market_context import (
    analyze_48h_oi_trend, detect_sideways_accumulation
)

logger = logging.getLogger(__name__)
EXCHANGES = ["binance", "okx", "bybit", "bitget"]


# ============================================================
# 做空信号权重（负值 = 降低做多概率，提高做空概率）
# ============================================================

SHORT_SIGNAL_WEIGHTS = {
    # 出货确认（最可靠，需要OI+价格共振）
    "oi_price_both_falling":    15,
    "obv_negative_divergence":  12,
    "double_top_confirmed":     12,
    "squeeze_exhausted":        10,
    "cmf_turning_negative":      9,
    "basis_collapsing":          8,

    # 做市商出货阶段信号
    "mm_distribution_phase":     8,
    "taker_sell_dominant":        6,
    "funding_flip_positive":      5,

    # 技术面看空
    "channel_breakdown":          7,
    "volume_surge_on_drop":       6,
}

MAX_SHORT_SIGNAL_SCORE = 60


# ============================================================
# 概率计算
# ============================================================

def calc_long_probability(triggered: list) -> float:
    """做多概率"""
    score = sum(SIGNAL_WEIGHTS.get(r["rule"], 0)
                for r in triggered
                if SIGNAL_WEIGHTS.get(r["rule"], 0) > 0)
    prob = BASE_PROBABILITY + (score / MAX_SIGNAL_SCORE) * 0.45
    return max(PROB_FLOOR, min(PROB_CAP, prob))


def calc_short_probability(short_triggered: list) -> float:
    """做空概率"""
    score = sum(SHORT_SIGNAL_WEIGHTS.get(r["rule"], 0)
                for r in short_triggered)
    prob = BASE_PROBABILITY + (score / MAX_SHORT_SIGNAL_SCORE) * 0.45
    return max(PROB_FLOOR, min(PROB_CAP, prob))


def resolve_direction(long_prob: float,
                       short_prob: float,
                       min_gap: float = 0.15) -> str:
    """
    解决多空冲突
    两者差距 < min_gap → 观望
    """
    gap = long_prob - short_prob
    if   gap >  min_gap:  return "LONG"
    elif gap < -min_gap:  return "SHORT"
    else:                 return "NEUTRAL"


def prob_to_level(prob: float) -> str:
    if   prob >= 0.75: return "HIGH"
    elif prob >= 0.62: return "MEDIUM"
    elif prob >= 0.55: return "WATCH"
    else:              return "NORMAL"


def prob_to_icon(prob: float) -> str:
    if   prob >= 0.80: return "🔴"
    elif prob >= 0.70: return "🟠"
    elif prob >= 0.60: return "🟡"
    else:              return "⚪"


# ============================================================
# 数据聚合
# ============================================================

def aggregate(token: str, raw: dict) -> dict:
    futures_prices = {}
    spot_prices    = {}
    ois            = {}
    fundings       = {}
    imbalances     = {}
    depths         = {}
    klines_data    = {}

    for ex in EXCHANGES:
        d  = raw.get(ex, {})
        ft = d.get("futures", {})
        sp = d.get("spot", {})

        bt = ft.get("book_ticker")
        if bt:
            futures_prices[ex] = bt.get("mid", 0)
            imbalances[ex]     = bt.get("imbalance", 0)

        oi = ft.get("oi")
        if oi:
            ois[ex] = oi.get("oi_usd") or oi.get("oi", 0)

        fr = ft.get("funding")
        if fr:
            fundings[ex] = fr.get("current", 0)

        dep = ft.get("depth")
        if dep:
            depths[ex] = dep

        kl = ft.get("klines")
        if kl:
            klines_data[ex] = kl

        sp_price = sp.get("price")
        if sp_price:
            spot_prices[ex] = (sp_price.get("price", 0)
                               if isinstance(sp_price, dict) else sp_price)

    # 合约跨所价差
    fp_list   = list(futures_prices.values())
    fp_median = statistics.median(fp_list) if fp_list else 0

    futures_deviations = {}
    if fp_median > 0:
        for ex, p in futures_prices.items():
            futures_deviations[ex] = (p - fp_median) / fp_median

    max_futures_spread = 0
    futures_outlier    = None
    if futures_deviations:
        max_futures_spread = max(abs(v) for v in futures_deviations.values())
        futures_outlier    = max(futures_deviations,
                                 key=lambda x: abs(futures_deviations[x]))

    # 现货
    sp_list  = list(spot_prices.values())
    spot_avg = statistics.mean(sp_list) if sp_list else 0

    # 基差
    basis = {}
    for ex in EXCHANGES:
        fp = futures_prices.get(ex)
        sp = spot_prices.get(ex) or spot_avg
        if fp and sp and sp > 0:
            basis[ex] = (fp - sp) / sp

    max_basis    = max((abs(v) for v in basis.values()), default=0)
    max_basis_ex = (max(basis, key=lambda x: abs(basis[x]))
                    if basis else None)

    # OI
    total_oi = sum(ois.values())
    oi_shares = {ex: v / total_oi for ex, v in ois.items()} \
                if total_oi > 0 else {}

    # 资金费率
    fv = list(fundings.values())
    funding_mean = statistics.mean(fv) if fv else 0
    funding_devs = {ex: v - funding_mean for ex, v in fundings.items()}

    # K线分析
    kline_analysis = {}
    for ex in ["binance", "bybit"]:
        kl = klines_data.get(ex, [])
        if kl and len(kl) >= 5:
            kline_analysis[ex] = analyze_klines(kl)

    return {
        "token":               token,
        "futures_prices":      futures_prices,
        "futures_deviations":  futures_deviations,
        "max_futures_spread":  max_futures_spread,
        "futures_outlier":     futures_outlier,
        "spot_prices":         spot_prices,
        "spot_avg":            spot_avg,
        "basis":               basis,
        "max_basis":           max_basis,
        "max_basis_ex":        max_basis_ex,
        "ois":                 ois,
        "total_oi":            total_oi,
        "oi_shares":           oi_shares,
        "fundings":            fundings,
        "funding_mean":        funding_mean,
        "funding_devs":        funding_devs,
        "imbalances":          imbalances,
        "depths":              depths,
        "kline_analysis":      kline_analysis,
        "raw":                 raw,
        "ts":                  int(time.time()),
    }


# ============================================================
# 规则引擎主体
# ============================================================

class RuleEngine:

    def run(self, token: str, agg: dict,
            snapshot_fn=None,
            coldstart_done: bool = True,
            market_context: dict = None) -> dict:

        market_context = market_context or {}

        # ── 做多规则 ──────────────────────────────────────
        long_triggered = []

        def add_long(results):
            long_triggered.extend(results)

        add_long(self._rule_futures_spread(token, agg))
        add_long(self._rule_basis(token, agg))
        add_long(self._rule_oi(token, agg, snapshot_fn))
        add_long(self._rule_funding_long(token, agg))
        add_long(self._rule_orderbook(token, agg, snapshot_fn))
        add_long(self._rule_liquidation_long(token, agg))
        add_long(self._rule_wash(token, agg))
        add_long(self._rule_ls_divergence(token, agg))
        add_long(self._rule_kline_long(token, agg))

        if snapshot_fn:
            add_long(self._rule_spread_persistence(token, agg, snapshot_fn))

        if coldstart_done and snapshot_fn:
            add_long(self._rule_twap(token, agg, snapshot_fn))

        # 新增：48h慢启动规则
        if snapshot_fn:
            add_long(self._rule_slow_accumulation(token, agg, snapshot_fn))
            add_long(self._rule_sideways_accumulation(token, agg, snapshot_fn))

        # ── 做空规则 ──────────────────────────────────────
        short_triggered = self._run_short_rules(token, agg, snapshot_fn)

        # ── 概率计算 ──────────────────────────────────────
        long_prob  = calc_long_probability(long_triggered)
        short_prob = calc_short_probability(short_triggered)
        direction  = resolve_direction(long_prob, short_prob)

        # ── 行为分类 ──────────────────────────────────────
        ka = agg.get("kline_analysis", {}).get("binance", {})
        classification = classify_behavior(
            agg=agg,
            market_context=market_context,
            kline_analysis=ka,
            snapshot_fn=snapshot_fn,
            token=token,
        )

        # ── 入场区间 ──────────────────────────────────────
        entry_zone = self._calc_entry_zone(
            token, agg, direction, short_triggered
        )

        # ── 阶段判断（整合行为分类）──────────────────────
        phase = self._phase(token, agg, long_triggered,
                            short_triggered, classification, snapshot_fn)

        # ── 主概率和级别 ──────────────────────────────────
        if direction == "LONG":
            probability = long_prob
            active_triggered = long_triggered
        elif direction == "SHORT":
            probability = short_prob
            active_triggered = short_triggered
        else:
            probability = max(long_prob, short_prob)
            active_triggered = long_triggered

        level = prob_to_level(probability)

        return {
            "token":            token,
            "direction":        direction,
            "probability":      probability,
            "long_probability": long_prob,
            "short_probability":short_prob,
            "level":            level,
            "phase":            phase,
            "triggered":        active_triggered,
            "long_triggered":   long_triggered,
            "short_triggered":  short_triggered,
            "classification":   classification,
            "entry_zone":       entry_zone,
            "agg":              agg,
            "score":            int(probability * 100),
            "ts":               int(time.time()),
        }

    # ──────────────────────────────────────────────────────
    # 做空规则集
    # ──────────────────────────────────────────────────────

    def _run_short_rules(self, token: str, agg: dict,
                          snapshot_fn) -> list:
        results = []

        # 1. OI+价格同步下跌（最强出货信号）
        oi_falling    = _check_oi_falling(snapshot_fn, token)
        price_falling = agg.get("kline_analysis", {}).get(
            "binance", {}).get("trend", {}).get("direction") == "downtrend"

        if oi_falling and price_falling:
            results.append({
                "rule":  "oi_price_both_falling",
                "level": "L1", "score": 15,
                "detail":"OI和价格同步下跌，做市商确认离场",
                "bullish": False,
            })

        # 2. OBV顶背离
        ka = agg.get("kline_analysis", {}).get("binance", {})
        if ka.get("obv_signals", {}).get("negative_divergence"):
            results.append({
                "rule":  "obv_negative_divergence",
                "level": "L1", "score": 12,
                "detail":"OBV顶背离（价格新高但资金未跟，上涨动能衰竭）",
                "bullish": False,
            })

        # 3. 双顶确认
        dt = ka.get("patterns", {}).get("double_top")
        if dt and dt.get("confirmed") and dt.get("obv_divergence"):
            results.append({
                "rule":  "double_top_confirmed",
                "level": "L1", "score": 12,
                "detail":f"双顶形态确认跌破颈线（OBV负背离✅）",
                "bullish": False,
            })

        # 4. 逼空结束
        fm         = agg.get("funding_mean", 0)
        taker_raw  = (agg.get("raw", {}).get("binance", {})
                      .get("futures", {}).get("taker_ratio") or {})
        taker_cur  = taker_raw.get("current", 1.0)
        was_squeeze= fm < -0.0003

        if (snapshot_fn
                and _was_previously_negative_funding(snapshot_fn, token)
                and fm >= 0
                and taker_cur < 0.9):
            results.append({
                "rule":  "squeeze_exhausted",
                "level": "L1", "score": 10,
                "detail":"逼空结束（资金费率翻正+卖方Taker主导），空头已出清",
                "bullish": False,
            })

        # 5. CMF高位转负
        cmf_cur  = ka.get("cmf_current", 0)
        cmf_turn = ka.get("cmf_turning", False)
        if cmf_cur < -0.1 and not cmf_turn:
            results.append({
                "rule":  "cmf_turning_negative",
                "level": "L1", "score": 9,
                "detail":f"CMF持续为负（{cmf_cur:.3f}），资金持续流出",
                "bullish": False,
            })

        # 6. 基差溢价消失（之前大幅溢价，现在收窄）
        max_basis = agg.get("max_basis", 0)
        if snapshot_fn:
            prev_basis = _get_prev_max_basis(snapshot_fn, token)
            if prev_basis > 0.02 and max_basis < 0.005:
                results.append({
                    "rule":  "basis_collapsing",
                    "level": "L2", "score": 8,
                    "detail":f"基差溢价从{prev_basis*100:.1f}%收窄至{max_basis*100:.1f}%，做市商出货完成",
                    "bullish": False,
                })

        # 7. Taker持续卖方主导
        taker_hist = taker_raw.get("history", [])
        if taker_hist:
            sell_dominant = sum(1 for r in taker_hist[-6:] if r < 0.85)
            if sell_dominant >= 5:
                results.append({
                    "rule":  "taker_sell_dominant",
                    "level": "L2", "score": 6,
                    "detail":f"Taker连续{sell_dominant}期卖方主导，持续抛压",
                    "bullish": False,
                })

        # 8. 资金费率翻正（逼空后）
        fundings = agg.get("fundings", {})
        all_positive = all(v > 0.0003 for v in fundings.values() if v)
        if all_positive and _was_previously_negative_funding(snapshot_fn, token):
            results.append({
                "rule":  "funding_flip_positive",
                "level": "L2", "score": 5,
                "detail":"全平台资金费率翻正，多头开始付费，逼空结束",
                "bullish": False,
            })

        # 9. 下行通道形成（做空延续信号）
        vol = ka.get("volume_analysis", {})
        if (price_falling
                and vol.get("vol_ratio", 1) > 1.3
                and ka.get("trend", {}).get("direction") == "downtrend"):
            results.append({
                "rule":  "volume_surge_on_drop",
                "level": "L2", "score": 6,
                "detail":"下跌时成交量放大，空方力量强",
                "bullish": False,
            })

        return results

    # ──────────────────────────────────────────────────────
    # 48h慢启动规则（新增，解决RECALL类漏报）
    # ──────────────────────────────────────────────────────

    def _rule_slow_accumulation(self, token: str, agg: dict,
                                 snapshot_fn) -> list:
        results = []
        oi_48h = analyze_48h_oi_trend(token, snapshot_fn)

        if not oi_48h.get("sufficient_data"):
            return []

        if oi_48h.get("slow_accumulation"):
            change = oi_48h.get("oi_change_48h", 0)
            results.append({
                "rule":  "slow_oi_accumulation",
                "level": "L2", "score": 9,
                "detail": (f"OI过去48h缓慢积累{change*100:.1f}%"
                          f"（单次最大变化{oi_48h.get('max_single_change',0)*100:.1f}%）"
                          f"，疑似主力悄悄建仓"),
            })

        return results

    def _rule_sideways_accumulation(self, token: str, agg: dict,
                                     snapshot_fn) -> list:
        results = []
        ka = agg.get("kline_analysis", {}).get("binance", {})
        sa = detect_sideways_accumulation(token, snapshot_fn, ka)

        if not sa.get("sufficient_data"):
            return []

        if sa.get("is_sideways_accum"):
            results.append({
                "rule":  "sideways_accumulation",
                "level": "L2", "score": 10,
                "detail": (f"低波动横盘（±{sa.get('price_range',0)*100:.1f}%）"
                          f"+卖方深度变薄+资金流入，疑似TWAP建仓早期"),
            })

        return results

    # ──────────────────────────────────────────────────────
    # 做多规则（保留v3，精简版）
    # ──────────────────────────────────────────────────────

    def _rule_futures_spread(self, token, agg) -> list:
        results = []
        spread  = agg.get("max_futures_spread", 0)
        outlier = agg.get("futures_outlier")
        devs    = agg.get("futures_deviations", {})

        if outlier:
            other = [abs(v) for ex, v in devs.items() if ex != outlier]
            outlier_dev = abs(devs.get(outlier, 0))
            if other and outlier_dev < statistics.mean(other) * 2:
                return []

        if   spread > 0.03:
            results.append({"rule": "futures_spread_L1", "level": "L1", "score": 3,
                "detail": f"跨所合约价差{spread*100:.2f}%，异常方:{outlier}"})
        elif spread > 0.005:
            results.append({"rule": "futures_spread_L2", "level": "L2", "score": 2,
                "detail": f"跨所合约价差{spread*100:.2f}%"})
        elif spread > 0.003:
            results.append({"rule": "futures_spread_L3", "level": "L3", "score": 1,
                "detail": f"跨所合约价差{spread*100:.2f}%"})
        return results

    def _rule_basis(self, token, agg) -> list:
        results  = []
        basis    = agg.get("basis", {})
        max_b    = agg.get("max_basis", 0)
        max_b_ex = agg.get("max_basis_ex")
        spot_avg = agg.get("spot_avg", 0)
        if not basis or spot_avg == 0:
            return []
        for ex, b in basis.items():
            ab = abs(b)
            d  = "合约溢价" if b > 0 else "合约折价"
            if   ab > BASIS_THRESHOLD["L1"]:
                results.append({"rule": "basis_L1", "level": "L1", "score": 3,
                    "detail": f"{ex} {d}{ab*100:.2f}%，合约严重脱离现货"})
            elif ab > BASIS_THRESHOLD["L2"]:
                results.append({"rule": "basis_L2", "level": "L2", "score": 2,
                    "detail": f"{ex} {d}{ab*100:.2f}%"})
            elif ab > BASIS_THRESHOLD["L3"]:
                results.append({"rule": "basis_L3", "level": "L3", "score": 1,
                    "detail": f"{ex} {d}{ab*100:.2f}%"})
        oi_shares = agg.get("oi_shares", {})
        spread    = agg.get("max_futures_spread", 0)
        if (max_b > 0.02
                and max(oi_shares.values(), default=0) > 0.40
                and spread > 0.003):
            results.append({"rule": "basis_manipulation", "level": "L1", "score": 3,
                "detail": f"基差{max_b*100:.2f}%+OI集中+价差持续，确认合约端拉盘"})
        return results

    def _rule_oi(self, token, agg, snapshot_fn) -> list:
        results  = []
        ois      = agg.get("ois", {})
        shares   = agg.get("oi_shares", {})
        total_oi = agg.get("total_oi", 0)
        for ex in EXCHANGES:
            if ex not in ois:
                continue
            if snapshot_fn:
                snaps = snapshot_fn(token, ex, limit=17)
                if len(snaps) >= 16:
                    old = snaps[-1].get("oi_usd", 0) or snaps[-1].get("oi", 0)
                    cur = ois[ex]
                    if old > 0:
                        change = (cur - old) / old
                        if   change > 0.50:
                            results.append({"rule": "oi_change_L1", "level": "L1",
                                "score": 4, "detail": f"{ex} OI 4h暴增{change*100:.1f}%"})
                        elif change > 0.20:
                            results.append({"rule": "oi_change_L2", "level": "L2",
                                "score": 2, "detail": f"{ex} OI 4h增加{change*100:.1f}%"})
            share = shares.get(ex, 0)
            if   share > 0.60:
                results.append({"rule": "oi_concentration_L1", "level": "L1",
                    "score": 2, "detail": f"{ex} OI占比{share*100:.1f}% 极度集中"})
            elif share > 0.45:
                results.append({"rule": "oi_concentration_L2", "level": "L2",
                    "score": 1, "detail": f"{ex} OI占比{share*100:.1f}%"})
        return results

    def _rule_funding_long(self, token, agg) -> list:
        results  = []
        fundings = agg.get("fundings", {})
        devs     = agg.get("funding_devs", {})
        for ex in EXCHANGES:
            fr = fundings.get(ex)
            if fr is None:
                continue
            ab  = abs(fr)
            dev = devs.get(ex, 0)
            if   ab > 0.001:
                results.append({"rule": "funding_abs_L1", "level": "L1", "score": 2,
                    "detail": f"{ex} 资金费率{fr*100:.4f}% 极端"})
            if   abs(dev) > 0.0008:
                results.append({"rule": "funding_dev_L1", "level": "L1", "score": 2,
                    "detail": f"{ex} 资金费率偏离均值{dev*100:.4f}%"})
            raw_fr = (agg["raw"].get(ex, {}).get("futures", {})
                      .get("funding", {}) or {})
            neg = raw_fr.get("negative_periods", 0)
            if   neg >= 5:
                results.append({"rule": "funding_persist_L1", "level": "L1", "score": 3,
                    "detail": f"{ex} 资金费率连续{neg}期为负"})
            elif neg >= 3:
                results.append({"rule": "funding_persist_L2", "level": "L2", "score": 1,
                    "detail": f"{ex} 资金费率连续{neg}期为负"})
        return results

    def _rule_orderbook(self, token, agg, snapshot_fn) -> list:
        results    = []
        depths     = agg.get("depths", {})
        imbalances = agg.get("imbalances", {})
        for ex in EXCHANGES:
            imb = imbalances.get(ex)
            if imb is None:
                continue
            if   imb > 0.7:
                results.append({"rule": "imbalance_L1", "level": "L1", "score": 4,
                    "detail": f"{ex} 失衡度{imb:.2f} 极度异常"})
            elif imb > 0.5:
                results.append({"rule": "imbalance_L2", "level": "L2", "score": 2,
                    "detail": f"{ex} 失衡度{imb:.2f}"})
            elif imb > 0.4:
                results.append({"rule": "imbalance_L3", "level": "L3", "score": 1,
                    "detail": f"{ex} 失衡度{imb:.2f}"})
            dep = depths.get(ex, {})
            if dep:
                for lb in dep.get("large_bids", []):
                    r = lb.get("ratio", 0)
                    if   r > 10:
                        results.append({"rule": "bid_wall_L1", "level": "L1", "score": 4,
                            "detail": f"{ex} 超大买单${lb['qty_usd']:,.0f}(均值{r:.1f}倍)"})
                    elif r > 5:
                        results.append({"rule": "bid_wall_L2", "level": "L2", "score": 2,
                            "detail": f"{ex} 大买单${lb['qty_usd']:,.0f}(均值{r:.1f}倍)"})
        return results

    def _rule_liquidation_long(self, token, agg) -> list:
        results = []
        tr  = (agg["raw"].get("binance", {}).get("futures", {})
               .get("taker_ratio") or {})
        cur = tr.get("current", 1.0)
        if   cur > 2.0:
            results.append({"rule": "liq_proxy_L1", "level": "L1", "score": 2,
                "detail": f"Taker买卖比{cur:.2f}，疑似空头爆仓"})
        elif cur > 1.5:
            results.append({"rule": "liq_proxy_L2", "level": "L2", "score": 1,
                "detail": f"Taker买卖比{cur:.2f}买方主导"})
        return results

    def _rule_wash(self, token, agg) -> list:
        results = []
        ticker  = (agg["raw"].get("binance", {}).get("futures", {})
                   .get("ticker_24h") or {})
        vol     = ticker.get("quote_vol_24h", 0) or 0
        total_oi= agg.get("total_oi", 0)
        if total_oi > 0 and vol > 0:
            ratio = vol / total_oi
            if   ratio > 30:
                results.append({"rule": "wash_L1", "level": "L1", "score": 3,
                    "detail": f"成交量/OI={ratio:.1f}x 高度疑似刷量"})
            elif ratio > 20:
                results.append({"rule": "wash_L2", "level": "L2", "score": 2,
                    "detail": f"成交量/OI={ratio:.1f}x 疑似刷量"})
        return results

    def _rule_ls_divergence(self, token, agg) -> list:
        results   = []
        top_ls    = (agg["raw"].get("binance", {}).get("futures", {})
                     .get("top_ls_ratio") or {})
        global_ls = (agg["raw"].get("binance", {}).get("futures", {})
                     .get("global_ls") or {})
        if not top_ls or not global_ls:
            return []
        top_long    = top_ls.get("top_long", False)
        retail_long = global_ls.get("retail_long", True)
        fm          = agg.get("funding_mean", 0)
        if top_long and not retail_long and fm < -0.0003:
            results.append({"rule": "ls_div_L1", "level": "L1", "score": 4,
                "detail": f"大户多头({top_ls['current']:.2f})+散户空头+负费率"})
        elif top_long and not retail_long:
            results.append({"rule": "ls_div_L2", "level": "L2", "score": 2,
                "detail": f"大户多头({top_ls['current']:.2f})vs散户空头"})
        return results

    def _rule_kline_long(self, token, agg) -> list:
        results        = []
        kline_analysis = agg.get("kline_analysis", {})
        ka = kline_analysis.get("binance") or kline_analysis.get("bybit")
        if not ka or ka.get("error"):
            return []
        for sig in ka.get("kline_signals", []):
            if not sig.get("bullish", True):
                continue
            rule   = sig["signal"]
            weight = SIGNAL_WEIGHTS.get(rule, sig.get("score", 0))
            if weight <= 0:
                continue
            results.append({
                "rule":    rule,
                "level":   "L1" if weight >= 8 else "L2",
                "score":   weight,
                "detail":  sig["desc"],
                "bullish": True,
                "source":  "kline",
            })
        return results

    def _rule_twap(self, token, agg, snapshot_fn) -> list:
        results = []
        trend = _field_trend(snapshot_fn, token, "binance", "imbalance", 10)
        if (trend["monotonic_rising"]
                and trend["change_pct"] > 0.03
                and len(trend["values"]) >= 8):
            results.append({"rule": "twap_creep", "level": "L1", "score": 4,
                "detail": f"失衡度连续{len(trend['values'])}次爬升 TWAP建仓指纹"})
        for ex in EXCHANGES:
            t = _field_trend(snapshot_fn, token, ex, "ask_depth_usd", 8)
            if (t["monotonic_falling"]
                    and t["change_pct"] < -0.08
                    and len(t["values"]) >= 6):
                results.append({"rule": "twap_ask_drain", "level": "L1", "score": 4,
                    "detail": f"{ex} 卖方深度连续{len(t['values'])}次下降"})
        tr   = (agg["raw"].get("binance", {}).get("futures", {})
                .get("taker_ratio") or {})
        hist = tr.get("history", [])
        ticker = (agg["raw"].get("binance", {}).get("futures", {})
                  .get("ticker_24h") or {})
        pc = ticker.get("change_pct_24h", 0) or 0
        if len(hist) >= 8:
            buy_dom = sum(1 for r in hist[-8:] if r > 1.0)
            if buy_dom >= 7 and abs(pc) < 0.02:
                results.append({"rule": "twap_buy_pressure", "level": "L2", "score": 3,
                    "detail": f"Taker买方连续{buy_dom}期主导但价格未大动"})
        return results

    def _rule_spread_persistence(self, token, agg, snapshot_fn) -> list:
        results  = []
        spread   = agg.get("max_futures_spread", 0)
        outlier  = agg.get("futures_outlier")
        if spread <= 0.003:
            return []
        sustained = _sustained_count(
            snapshot_fn, token, "binance", "max_spread", 0.003, "above", 12)
        if sustained >= 3:
            results.append({"rule": "spread_persist_L1", "level": "L1", "score": 2,
                "detail": f"价差>0.3%持续{sustained*15}分钟"})
        return results

    # ──────────────────────────────────────────────────────
    # 入场区间计算（支持做空方向）
    # ──────────────────────────────────────────────────────

    def _calc_entry_zone(self, token, agg, direction, short_triggered) -> dict:
        spot_avg  = agg.get("spot_avg", 0)
        fp        = agg.get("futures_prices", {})
        curr      = fp.get("binance", 0) or (sum(fp.values())/len(fp) if fp else 0)
        if not curr:
            return {}

        ka  = agg.get("kline_analysis", {}).get("binance", {})
        sr  = ka.get("support_resistance", {}) if ka else {}
        poc = sr.get("levels", {}).get("poc", 0) if sr else 0

        if direction == "LONG":
            entry_low  = spot_avg * 1.002 if spot_avg else curr * 0.99
            entry_high = curr
            stop_loss  = poc * 0.97 if poc else spot_avg * 0.97
            target_1   = curr * 1.08
            target_2   = curr * 1.20
            window     = "1~4小时"
            return {
                "direction":  "LONG",
                "entry_low":  entry_low,
                "entry_high": entry_high,
                "stop_loss":  stop_loss,
                "target_1":   target_1,
                "target_2":   target_2,
                "window":     window,
            }

        elif direction == "SHORT":
            entry_high = curr
            entry_low  = curr * 0.99
            stop_loss  = curr * 1.04    # 止损在入场价上方4%
            target_1   = curr * 0.92    # 目标一：-8%
            target_2   = curr * 0.85    # 目标二：-15%
            window     = "4~12小时"
            return {
                "direction":  "SHORT",
                "entry_low":  entry_low,
                "entry_high": entry_high,
                "stop_loss":  stop_loss,
                "target_1":   target_1,
                "target_2":   target_2,
                "window":     window,
                "warning":    ("⚠️ 低流通代币做空风险极高\n"
                              "务必严格执行止损，仓位不超过总资金5%"),
            }

        return {}

    # ──────────────────────────────────────────────────────
    # 阶段判断
    # ──────────────────────────────────────────────────────

    def _phase(self, token, agg, long_triggered, short_triggered,
               classification, snapshot_fn) -> str:

        btype = classification.get("behavior_type", "UNKNOWN")
        mphase= classification.get("mm_phase_key", "UNKNOWN")

        phase_map = {
            ("SQUEEZE",       "MARKUP"):       "🔴 逼空进行中",
            ("SQUEEZE",       "ACCUMULATION"): "🔵 逼空蓄力中",
            ("TWAP_ACCUM",    "ACCUMULATION"): "🔵 TWAP建仓中",
            ("STEALTH_ACCUM", "ACCUMULATION"): "🔵 隐秘建仓中",
            ("DISTRIBUTION",  "DISTRIBUTION"): "🟡 出货进行中",
            ("PUMP_DUMP",     "DISTRIBUTION"): "⚠️ 拉高出货",
            ("PUMP_DUMP",     "MARKUP"):       "🟠 拉盘进行中",
            ("MOMENTUM_RIDE", "MARKUP"):       "🟠 借势拉盘",
            ("WASHOUT",       "WASHOUT"):      "🟡 洗盘震仓",
            ("STOP_HUNT",     "STOP_HUNT_LONG"):  "⚡ 猎杀多头",
            ("STOP_HUNT",     "STOP_HUNT_SHORT"): "⚡ 猎杀空头",
            ("REACTIVE",      "UNKNOWN"):      "〰️ 市场跟随",
        }

        phase = phase_map.get((btype, mphase))
        if phase:
            return phase

        # 兜底逻辑
        if short_triggered and len(short_triggered) >= 3:
            return "🔴 出货信号"
        if long_triggered and len(long_triggered) >= 3:
            return "🔵 看多信号"
        return "⚫ 信号混合"


# ============================================================
# 辅助函数
# ============================================================

def _sustained_count(snapshot_fn, token, exchange, field,
                     threshold, direction, limit) -> int:
    snaps = snapshot_fn(token, exchange, limit=limit)
    count = 0
    for snap in snaps:
        val = snap.get(field)
        if val is None:
            break
        if direction == "above" and val >= threshold:
            count += 1
        elif direction == "below" and val <= threshold:
            count += 1
        else:
            break
    return count


def _field_trend(snapshot_fn, token, exchange, field, limit) -> dict:
    snaps  = snapshot_fn(token, exchange, limit=limit)
    values = [s.get(field) for s in snaps if s.get(field) is not None]
    values_asc = list(reversed(values))
    if not values_asc:
        return {"values": [], "trend": "flat", "change_pct": 0,
                "monotonic_rising": False, "monotonic_falling": False}
    change = ((values_asc[-1] - values_asc[0]) / abs(values_asc[0])
              if values_asc[0] != 0 else 0)
    return {
        "values":            values_asc,
        "trend":             "rising" if values_asc[-1] > values_asc[0] else "falling",
        "change_pct":        change,
        "monotonic_rising":  all(values_asc[i] >= values_asc[i-1]
                                 for i in range(1, len(values_asc))),
        "monotonic_falling": all(values_asc[i] <= values_asc[i-1]
                                 for i in range(1, len(values_asc))),
    }


def _check_oi_falling(snapshot_fn, token) -> bool:
    if not snapshot_fn:
        return False
    snaps = snapshot_fn(token, "binance", limit=5)
    if len(snaps) < 2:
        return False
    old = snaps[-1].get("total_oi", 0)
    new = snaps[0].get("total_oi", 0)
    return new < old * 0.95 if old > 0 else False


def _was_previously_negative_funding(snapshot_fn, token) -> bool:
    """检查过去是否有负资金费率（判断逼空是否结束）"""
    if not snapshot_fn:
        return False
    snaps = snapshot_fn(token, "binance", limit=10)
    return any(s.get("funding", 0) < -0.0002 for s in snaps[1:])


def _get_prev_max_basis(snapshot_fn, token) -> float:
    """获取之前的最大基差"""
    if not snapshot_fn:
        return 0
    snaps = snapshot_fn(token, "binance", limit=10)
    if len(snaps) < 2:
        return 0
    return max((s.get("max_basis", 0) for s in snaps[1:]), default=0)
