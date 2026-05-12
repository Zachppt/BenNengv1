# ============================================================
# rules/engine.py — v3
# 从"异动评分制"升级为"上涨概率制"
# 整合K线分析模块
# ============================================================

import time
import logging
import statistics
from typing import Optional
from config import (
    SIGNAL_WEIGHTS, BASE_PROBABILITY, MAX_SIGNAL_SCORE,
    PROB_CAP, PROB_FLOOR, BASIS_THRESHOLD
)
from rules.kline_analyzer import analyze_klines, format_kline_summary

logger = logging.getLogger(__name__)
EXCHANGES = ["binance", "okx", "bybit", "bitget"]


# ============================================================
# 概率计算
# ============================================================

def calc_probability(triggered: list) -> float:
    """
    把触发的规则列表转换为上涨概率
    正信号提高概率，负信号降低概率
    """
    signal_score = sum(
        SIGNAL_WEIGHTS.get(r["rule"], 0)
        for r in triggered
    )
    prob = BASE_PROBABILITY + (signal_score / MAX_SIGNAL_SCORE) * 0.45
    return max(PROB_FLOOR, min(PROB_CAP, prob))


def prob_to_level(prob: float) -> str:
    if   prob >= 0.75:
        return "HIGH"
    elif prob >= 0.62:
        return "MEDIUM"
    elif prob >= 0.55:
        return "WATCH"
    else:
        return "NORMAL"


def prob_to_icon(prob: float) -> str:
    if   prob >= 0.80:
        return "🔴"
    elif prob >= 0.70:
        return "🟠"
    elif prob >= 0.60:
        return "🟡"
    else:
        return "⚪"


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
                               if isinstance(sp_price, dict)
                               else sp_price)

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

    # 现货均价
    sp_list  = list(spot_prices.values())
    spot_avg = statistics.mean(sp_list) if sp_list else 0

    # 基差
    basis    = {}
    basis_usd= {}
    for ex in EXCHANGES:
        fp = futures_prices.get(ex)
        sp = spot_prices.get(ex) or spot_avg
        if fp and sp and sp > 0:
            basis[ex]     = (fp - sp) / sp
            basis_usd[ex] = fp - sp

    max_basis    = max((abs(v) for v in basis.values()), default=0)
    max_basis_ex = (max(basis, key=lambda x: abs(basis[x]))
                    if basis else None)

    # OI 分布
    total_oi = sum(ois.values())
    oi_shares = {ex: v / total_oi for ex, v in ois.items()} if total_oi > 0 else {}

    # 资金费率
    fv = list(fundings.values())
    funding_mean = statistics.mean(fv) if fv else 0
    funding_devs = {ex: v - funding_mean for ex, v in fundings.items()}

    # K线技术分析（用Binance数据为主）
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
        "basis_usd":           basis_usd,
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
            coldstart_done: bool = True) -> dict:

        triggered = []

        def add(results):
            triggered.extend(results)

        # 衍生品规则
        add(self._rule_futures_spread(token, agg))
        add(self._rule_basis(token, agg))
        add(self._rule_oi(token, agg, snapshot_fn))
        add(self._rule_funding(token, agg))
        add(self._rule_orderbook(token, agg, snapshot_fn))
        add(self._rule_liquidation(token, agg))
        add(self._rule_wash(token, agg))
        add(self._rule_ls_divergence(token, agg))

        if snapshot_fn:
            add(self._rule_spread_persistence(token, agg, snapshot_fn))

        if coldstart_done and snapshot_fn:
            add(self._rule_twap(token, agg, snapshot_fn))

        # K线技术分析规则（新增）
        add(self._rule_kline(token, agg))

        # 计算上涨概率
        probability = calc_probability(triggered)
        level       = prob_to_level(probability)
        phase       = self._phase(token, agg, triggered, snapshot_fn)

        # 入场区间计算
        entry_zone  = self._calc_entry_zone(token, agg)

        return {
            "token":       token,
            "probability": probability,
            "level":       level,
            "phase":       phase,
            "triggered":   triggered,
            "entry_zone":  entry_zone,
            "agg":         agg,
            "ts":          int(time.time()),
            # 兼容旧字段
            "score":       int(probability * 100),
        }

    # ──────────────────────────────────────────────────────
    # K线技术分析规则（新增）
    # ──────────────────────────────────────────────────────

    def _rule_kline(self, token: str, agg: dict) -> list:
        results        = []
        kline_analysis = agg.get("kline_analysis", {})

        # 用Binance数据为主，Bybit为辅
        ka = kline_analysis.get("binance") or kline_analysis.get("bybit")
        if not ka or ka.get("error"):
            return []

        # 直接使用kline_analyzer已计算的信号
        for sig in ka.get("kline_signals", []):
            rule = sig["signal"]
            weight = SIGNAL_WEIGHTS.get(rule, 0)
            if weight == 0:
                weight = sig.get("score", 0)

            results.append({
                "rule":     rule,
                "level":    "L1" if abs(weight) >= 8 else "L2",
                "score":    weight,
                "detail":   sig["desc"],
                "bullish":  sig.get("bullish", True),
                "source":   "kline",
            })

        return results

    # ──────────────────────────────────────────────────────
    # 规则 1：跨所合约价差
    # ──────────────────────────────────────────────────────

    def _rule_futures_spread(self, token: str, agg: dict) -> list:
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
                "detail": f"跨所合约价差{spread*100:.2f}%，异常方:{outlier}"})
        elif spread > 0.003:
            results.append({"rule": "futures_spread_L3", "level": "L3", "score": 1,
                "detail": f"跨所合约价差{spread*100:.2f}%，进入观察"})
        return results

    # ──────────────────────────────────────────────────────
    # 规则 2：现货-合约基差
    # ──────────────────────────────────────────────────────

    def _rule_basis(self, token: str, agg: dict) -> list:
        results  = []
        basis    = agg.get("basis", {})
        max_b    = agg.get("max_basis", 0)
        max_b_ex = agg.get("max_basis_ex")
        spot_avg = agg.get("spot_avg", 0)

        if not basis or spot_avg == 0:
            return []

        for ex, b in basis.items():
            ab = abs(b)
            direction = "合约溢价" if b > 0 else "合约折价"
            if   ab > BASIS_THRESHOLD["L1"]:
                results.append({"rule": "basis_L1", "level": "L1", "score": 3,
                    "detail": f"{ex} {direction}{ab*100:.2f}%，合约严重脱离现货",
                    "exchange": ex})
            elif ab > BASIS_THRESHOLD["L2"]:
                results.append({"rule": "basis_L2", "level": "L2", "score": 2,
                    "detail": f"{ex} {direction}{ab*100:.2f}%", "exchange": ex})
            elif ab > BASIS_THRESHOLD["L3"]:
                results.append({"rule": "basis_L3", "level": "L3", "score": 1,
                    "detail": f"{ex} {direction}{ab*100:.2f}%", "exchange": ex})

        oi_shares = agg.get("oi_shares", {})
        spread    = agg.get("max_futures_spread", 0)
        if (max_b > 0.02
                and max(oi_shares.values(), default=0) > 0.40
                and spread > 0.003):
            results.append({"rule": "basis_manipulation", "level": "L1", "score": 3,
                "detail": f"基差{max_b*100:.2f}%+OI集中+价差持续，确认合约端拉盘，主场:{max_b_ex}"})

        return results

    # ──────────────────────────────────────────────────────
    # 规则 3：OI
    # ──────────────────────────────────────────────────────

    def _rule_oi(self, token: str, agg: dict, snapshot_fn) -> list:
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
                                "score": 4, "detail": f"{ex} OI 4h暴增{change*100:.1f}%",
                                "exchange": ex})
                        elif change > 0.20:
                            results.append({"rule": "oi_change_L2", "level": "L2",
                                "score": 2, "detail": f"{ex} OI 4h增加{change*100:.1f}%",
                                "exchange": ex})

            share = shares.get(ex, 0)
            if   share > 0.60:
                results.append({"rule": "oi_concentration_L1", "level": "L1",
                    "score": 2, "detail": f"{ex} OI占比{share*100:.1f}% 极度集中",
                    "exchange": ex})
            elif share > 0.45:
                results.append({"rule": "oi_concentration_L2", "level": "L2",
                    "score": 1, "detail": f"{ex} OI占比{share*100:.1f}%", "exchange": ex})

        return results

    # ──────────────────────────────────────────────────────
    # 规则 4：资金费率
    # ──────────────────────────────────────────────────────

    def _rule_funding(self, token: str, agg: dict) -> list:
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
                    "detail": f"{ex} 资金费率{fr*100:.4f}% 极端", "exchange": ex})
            elif ab > 0.0005:
                results.append({"rule": "funding_abs_L2", "level": "L2", "score": 1,
                    "detail": f"{ex} 资金费率{fr*100:.4f}%", "exchange": ex})

            if   abs(dev) > 0.0008:
                results.append({"rule": "funding_dev_L1", "level": "L1", "score": 2,
                    "detail": f"{ex} 资金费率偏离均值{dev*100:.4f}%", "exchange": ex})
            elif abs(dev) > 0.0004:
                results.append({"rule": "funding_dev_L2", "level": "L2", "score": 1,
                    "detail": f"{ex} 资金费率偏离{dev*100:.4f}%", "exchange": ex})

            raw_fr = (agg["raw"].get(ex, {}).get("futures", {})
                      .get("funding", {}) or {})
            neg    = raw_fr.get("negative_periods", 0)
            if   neg >= 5:
                results.append({"rule": "funding_persist_L1", "level": "L1", "score": 3,
                    "detail": f"{ex} 资金费率连续{neg}期为负", "exchange": ex})
            elif neg >= 3:
                results.append({"rule": "funding_persist_L2", "level": "L2", "score": 1,
                    "detail": f"{ex} 资金费率连续{neg}期为负", "exchange": ex})

        return results

    # ──────────────────────────────────────────────────────
    # 规则 5：订单簿
    # ──────────────────────────────────────────────────────

    def _rule_orderbook(self, token: str, agg: dict, snapshot_fn) -> list:
        results    = []
        depths     = agg.get("depths", {})
        imbalances = agg.get("imbalances", {})

        for ex in EXCHANGES:
            imb = imbalances.get(ex)
            if imb is None:
                continue

            if   imb > 0.7:
                results.append({"rule": "imbalance_L1", "level": "L1", "score": 4,
                    "detail": f"{ex} 失衡度{imb:.2f} 极度异常", "exchange": ex})
            elif imb > 0.5:
                results.append({"rule": "imbalance_L2", "level": "L2", "score": 2,
                    "detail": f"{ex} 失衡度{imb:.2f}", "exchange": ex})
            elif imb > 0.4:
                results.append({"rule": "imbalance_L3", "level": "L3", "score": 1,
                    "detail": f"{ex} 失衡度{imb:.2f}", "exchange": ex})

            if snapshot_fn:
                sustained = _sustained_count(
                    snapshot_fn, token, ex, "imbalance", 0.4, "above", 12)
                if sustained >= 6:
                    results.append({"rule": "imbalance_persist_L1", "level": "L1",
                        "score": 1, "detail": f"{ex} 失衡度>0.4持续{sustained*15}分钟",
                        "exchange": ex})

            dep = depths.get(ex, {})
            if dep:
                for lb in dep.get("large_bids", []):
                    r = lb.get("ratio", 0)
                    if   r > 10:
                        results.append({"rule": "bid_wall_L1", "level": "L1", "score": 4,
                            "detail": f"{ex} 超大买单${lb['qty_usd']:,.0f}(均值{r:.1f}倍)",
                            "exchange": ex})
                    elif r > 5:
                        results.append({"rule": "bid_wall_L2", "level": "L2", "score": 2,
                            "detail": f"{ex} 大买单${lb['qty_usd']:,.0f}(均值{r:.1f}倍)",
                            "exchange": ex})

        return results

    # ──────────────────────────────────────────────────────
    # 规则 6：TWAP
    # ──────────────────────────────────────────────────────

    def _rule_twap(self, token: str, agg: dict, snapshot_fn) -> list:
        results = []

        trend = _field_trend(snapshot_fn, token, "binance", "imbalance", 10)
        if (trend["monotonic_rising"]
                and trend["change_pct"] > 0.03
                and len(trend["values"]) >= 8):
            results.append({"rule": "twap_creep", "level": "L1", "score": 4,
                "detail": (f"失衡度连续{len(trend['values'])}次单向爬升"
                           f"(+{trend['change_pct']*100:.1f}%) TWAP建仓指纹")})

        for ex in EXCHANGES:
            t = _field_trend(snapshot_fn, token, ex, "ask_depth_usd", 8)
            if (t["monotonic_falling"]
                    and t["change_pct"] < -0.08
                    and len(t["values"]) >= 6):
                results.append({"rule": "twap_ask_drain", "level": "L1", "score": 4,
                    "detail": f"{ex} 卖方深度连续{len(t['values'])}次下降"
                              f"({t['change_pct']*100:.1f}%)",
                    "exchange": ex})

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
                    "detail": f"Taker买方连续{buy_dom}期主导但价格未大动，疑似吸筹"})

        return results

    # ──────────────────────────────────────────────────────
    # 规则 7：爆仓代理
    # ──────────────────────────────────────────────────────

    def _rule_liquidation(self, token: str, agg: dict) -> list:
        results = []
        tr  = (agg["raw"].get("binance", {}).get("futures", {})
               .get("taker_ratio") or {})
        cur = tr.get("current", 1.0)
        if   cur > 2.0:
            results.append({"rule": "liq_proxy_L1", "level": "L1", "score": 2,
                "detail": f"Taker买卖比{cur:.2f}，疑似空头爆仓级联"})
        elif cur > 1.5:
            results.append({"rule": "liq_proxy_L2", "level": "L2", "score": 1,
                "detail": f"Taker买卖比{cur:.2f}买方主导"})
        return results

    # ──────────────────────────────────────────────────────
    # 规则 8：洗盘刷量
    # ──────────────────────────────────────────────────────

    def _rule_wash(self, token: str, agg: dict) -> list:
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

        for ex, share in agg.get("oi_shares", {}).items():
            if share > 0.60:
                results.append({"rule": "wash_concentration", "level": "L1", "score": 2,
                    "detail": f"{ex} OI占比{share*100:.1f}%，疑似集中刷量",
                    "exchange": ex})

        return results

    # ──────────────────────────────────────────────────────
    # 规则 9：大户 vs 散户背离
    # ──────────────────────────────────────────────────────

    def _rule_ls_divergence(self, token: str, agg: dict) -> list:
        results   = []
        top_ls    = (agg["raw"].get("binance", {}).get("futures", {})
                     .get("top_ls_ratio") or {})
        global_ls = (agg["raw"].get("binance", {}).get("futures", {})
                     .get("global_ls") or {})
        if not top_ls or not global_ls:
            return []

        top_long    = top_ls.get("top_long", False)
        retail_long = global_ls.get("retail_long", False)
        fm          = agg.get("funding_mean", 0)

        if top_long and not retail_long and fm < -0.0003:
            results.append({"rule": "ls_div_L1", "level": "L1", "score": 4,
                "detail": (f"大户多头({top_ls['current']:.2f})"
                           f"+散户空头({global_ls['current']:.2f})"
                           f"+负费率 经典逼空设置")})
        elif top_long and not retail_long:
            results.append({"rule": "ls_div_L2", "level": "L2", "score": 2,
                "detail": (f"大户多头({top_ls['current']:.2f})"
                           f"vs 散户空头({global_ls['current']:.2f})")})
        return results

    # ──────────────────────────────────────────────────────
    # 规则 10：价差持续时间
    # ──────────────────────────────────────────────────────

    def _rule_spread_persistence(self, token: str, agg: dict,
                                  snapshot_fn) -> list:
        results  = []
        spread   = agg.get("max_futures_spread", 0)
        outlier  = agg.get("futures_outlier")
        if spread <= 0.003:
            return []

        sustained = _sustained_count(
            snapshot_fn, token, "binance", "max_spread", 0.003, "above", 12)
        if   sustained >= 3:
            results.append({"rule": "spread_persist_L1", "level": "L1", "score": 2,
                "detail": f"价差>0.3%持续{sustained*15}分钟，{outlier}流动性被控制",
                "exchange": outlier})
        elif sustained >= 1:
            results.append({"rule": "spread_persist_L2", "level": "L2", "score": 1,
                "detail": f"价差>0.3%持续{sustained*15}分钟", "exchange": outlier})
        return results

    # ──────────────────────────────────────────────────────
    # 阶段判断
    # ──────────────────────────────────────────────────────

    def _phase(self, token: str, agg: dict,
               triggered: list, snapshot_fn) -> str:
        rules     = {r["rule"] for r in triggered}
        fm        = agg.get("funding_mean", 0)
        imbs      = agg.get("imbalances", {})
        max_imb   = max(imbs.values(), default=0)
        tr        = (agg["raw"].get("binance", {}).get("futures", {})
                     .get("taker_ratio") or {})
        cur_taker = tr.get("current", 1.0)
        max_basis = agg.get("max_basis", 0)

        # K线阶段信号
        ka = agg.get("kline_analysis", {}).get("binance", {})
        trend_dir = ka.get("trend", {}).get("direction", "unknown")
        has_breakout = any(
            r in rules for r in
            ["channel_breakout", "flag_breakout", "volume_surge_breakout"]
        )
        has_bottom = any(
            r in rules for r in ["double_bottom", "obv_divergence_pos"]
        )
        has_top = any(
            r in rules for r in ["double_top", "obv_divergence_neg"]
        )

        # 前序阶段
        prev = None
        if snapshot_fn:
            try:
                from cache.snapshot import get_previous_phase
                prev = get_previous_phase(token)
            except Exception:
                pass

        # 突破启动（K线确认）
        if has_breakout and trend_dir != "downtrend":
            if fm < -0.0003 or cur_taker > 1.5:
                return "🚀 突破启动中"
            return "📈 突破待确认"

        # 底部反转
        if has_bottom and trend_dir == "downtrend":
            return "🔄 底部反转信号"

        # 逼空进行中
        squeeze = sum([
            fm < -0.0003,
            cur_taker > 1.5,
            any("liq_proxy_L1" in r for r in rules),
            any("oi_change_L1" in r for r in rules),
        ])
        if squeeze >= 3:
            return "🔴 逼空进行中"

        # 逼空收尾
        if prev == "🔴 逼空进行中" and fm >= 0 and cur_taker < 1.3:
            return "🟡 逼空收尾"

        # 出货信号
        if has_top:
            return "⚠️ 顶部出货信号"

        if max_basis > 0.02 and cur_taker < 0.9:
            return "🟡 出货进行中"

        # 逼空蓄力
        buildup = sum([
            fm < -0.0003,
            any("funding_persist_L1" in r for r in rules),
            max_imb > 0.3,
            any("twap_ask_drain" in r for r in rules),
        ])
        if buildup >= 3:
            return "🔵 逼空蓄力中"

        # 建仓中
        accum = sum([
            any("twap_creep" in r for r in rules),
            any("obv_breakout" in r for r in rules),
            any("cmf_turning" in r for r in rules),
            max_imb > 0.4,
        ])
        if accum >= 2:
            return "🔵 建仓中"

        return "⚫ 信号混合"

    # ──────────────────────────────────────────────────────
    # 入场区间计算
    # ──────────────────────────────────────────────────────

    def _calc_entry_zone(self, token: str, agg: dict) -> dict:
        """
        基于现货价格、VPVR支撑位、OI变化计算入场区间
        """
        spot_avg  = agg.get("spot_avg", 0)
        fp        = agg.get("futures_prices", {})
        curr_price= fp.get("binance", 0) or (sum(fp.values())/len(fp) if fp else 0)

        if not curr_price or not spot_avg:
            return {}

        # 关键支撑位：VPVR POC
        ka  = agg.get("kline_analysis", {}).get("binance", {})
        sr  = ka.get("support_resistance", {}) if ka else {}
        poc = sr.get("levels", {}).get("poc", 0) if sr else 0
        val = sr.get("levels", {}).get("val", 0) if sr else 0

        # 入场区间
        entry_low  = max(spot_avg * 1.002, val * 0.998) if val else spot_avg * 1.002
        entry_high = curr_price

        # 止损：POC下方3%或现货价格下方3%
        if poc and poc > 0:
            stop_loss = poc * 0.97
        else:
            stop_loss = spot_avg * 0.97

        # 目标价：基于OI积累
        ois    = agg.get("ois", {})
        shares = agg.get("oi_shares", {})
        max_ex = max(shares, key=shares.get) if shares else None
        oi_surge = 0

        if max_ex and len(agg.get("raw", {}).get(max_ex, {}).get("futures", {})
                          .get("oi", {}) or {}) > 0:
            oi_surge = shares.get(max_ex, 0)

        target_1 = curr_price * 1.08   # 保守目标
        target_2 = curr_price * min(1 + oi_surge * 0.6, 1.30)  # 激进目标

        # 预计时间窗口（基于信号强度）
        ka_score = ka.get("kline_score", 0) if ka else 0
        if ka_score >= 15:
            window = "0.5~2小时"
        elif ka_score >= 8:
            window = "1~4小时"
        else:
            window = "4~12小时"

        return {
            "entry_low":  entry_low,
            "entry_high": entry_high,
            "stop_loss":  stop_loss,
            "target_1":   target_1,
            "target_2":   target_2,
            "poc":        poc,
            "window":     window,
            "spot_avg":   spot_avg,
        }


# ============================================================
# 快照辅助函数
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
        "trend":             ("rising"  if values_asc[-1] > values_asc[0]
                              else "falling" if values_asc[-1] < values_asc[0]
                              else "flat"),
        "change_pct":        change,
        "monotonic_rising":  all(values_asc[i] >= values_asc[i-1]
                                 for i in range(1, len(values_asc))),
        "monotonic_falling": all(values_asc[i] <= values_asc[i-1]
                                 for i in range(1, len(values_asc))),
    }
