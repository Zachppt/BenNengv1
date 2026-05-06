# ============================================================
# rules/engine.py — 规则引擎 v2
# 新增：现货基差规则、多空双杀、定点清算、流动性猎杀（插针）
# ============================================================

import time
import logging
import statistics
from typing import Optional
from config import BASIS_THRESHOLD, NOISE_THRESHOLD

logger = logging.getLogger(__name__)

EXCHANGES = ["binance", "okx", "bybit", "bitget"]


# ============================================================
# 数据聚合
# ============================================================

def aggregate(token: str, raw: dict) -> dict:
    """
    把 fetch_token_full() 返回的 raw 整合成规则引擎可用的结构
    新增：现货价格聚合、基差计算
    """
    futures_prices = {}
    spot_prices    = {}
    ois            = {}
    fundings       = {}
    imbalances     = {}
    depths         = {}
    klines         = {}

    for ex in EXCHANGES:
        d = raw.get(ex, {})

        # 合约数据
        ft = d.get("futures", {})
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
            klines[ex] = kl

        # 现货数据
        sp = d.get("spot", {})
        sp_price = sp.get("price")
        if sp_price:
            spot_prices[ex] = (sp_price.get("price", 0)
                               if isinstance(sp_price, dict)
                               else sp_price)

    # ── 合约跨所价差 ──
    fp_list = list(futures_prices.values())
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

    # ── 现货跨所价差 ──
    sp_list   = list(spot_prices.values())
    sp_median = statistics.median(sp_list) if sp_list else 0

    spot_deviations  = {}
    if sp_median > 0:
        for ex, p in spot_prices.items():
            spot_deviations[ex] = (p - sp_median) / sp_median

    # ── 现货-合约基差（核心新增）──
    basis = {}          # exchange → basis_pct
    basis_usd = {}      # exchange → basis_usd
    spot_avg = statistics.mean(sp_list) if sp_list else 0

    for ex in EXCHANGES:
        fp = futures_prices.get(ex)
        sp = spot_prices.get(ex) or spot_avg
        if fp and sp and sp > 0:
            b = (fp - sp) / sp
            basis[ex]     = b
            basis_usd[ex] = fp - sp

    max_basis     = max((abs(v) for v in basis.values()), default=0)
    max_basis_ex  = (max(basis, key=lambda x: abs(basis[x]))
                     if basis else None)

    # ── OI 分布 ──
    total_oi = sum(ois.values())
    oi_shares = ({ex: v / total_oi for ex, v in ois.items()}
                 if total_oi > 0 else {})

    # ── 资金费率 ──
    fv = list(fundings.values())
    funding_mean = statistics.mean(fv) if fv else 0
    funding_devs = {ex: v - funding_mean for ex, v in fundings.items()}

    return {
        "token":              token,
        # 合约
        "futures_prices":     futures_prices,
        "futures_deviations": futures_deviations,
        "max_futures_spread": max_futures_spread,
        "futures_outlier":    futures_outlier,
        # 现货
        "spot_prices":        spot_prices,
        "spot_avg":           spot_avg,
        "spot_deviations":    spot_deviations,
        # 基差
        "basis":              basis,
        "basis_usd":          basis_usd,
        "max_basis":          max_basis,
        "max_basis_ex":       max_basis_ex,
        # OI
        "ois":                ois,
        "total_oi":           total_oi,
        "oi_shares":          oi_shares,
        # 资金费率
        "fundings":           fundings,
        "funding_mean":       funding_mean,
        "funding_devs":       funding_devs,
        # 订单簿
        "imbalances":         imbalances,
        "depths":             depths,
        # K 线
        "klines":             klines,
        # 原始数据
        "raw":                raw,
        "ts":                 int(time.time()),
    }


# ============================================================
# 规则引擎主体
# ============================================================

class RuleEngine:

    def run(self, token: str, agg: dict,
            snapshot_fn=None, coldstart_done: bool = True) -> dict:
        """
        运行所有规则，返回评分结构
        snapshot_fn: 可选，传入快照查询函数（用于时序规则）
        """
        triggered = []
        score     = 0

        def add(results):
            nonlocal score
            triggered.extend(results)
            score += sum(r["score"] for r in results)

        # ── 1. 跨所合约价差 ──────────────────────────────
        add(self._rule_futures_spread(token, agg))

        # ── 2. 现货-合约基差（新增）─────────────────────
        add(self._rule_basis(token, agg))

        # ── 3. OI ────────────────────────────────────────
        add(self._rule_oi(token, agg, snapshot_fn))

        # ── 4. 资金费率 ──────────────────────────────────
        add(self._rule_funding(token, agg))

        # ── 5. 订单簿 ────────────────────────────────────
        add(self._rule_orderbook(token, agg, snapshot_fn))

        # ── 6. TWAP（需要快照历史）──────────────────────
        if coldstart_done and snapshot_fn:
            add(self._rule_twap(token, agg, snapshot_fn))

        # ── 7. 爆仓代理 ──────────────────────────────────
        add(self._rule_liquidation(token, agg))

        # ── 8. 洗盘刷量 ──────────────────────────────────
        add(self._rule_wash(token, agg))

        # ── 9. 大户 vs 散户背离 ─────────────────────────
        add(self._rule_ls_divergence(token, agg))

        # ── 10. 价差持续时间 ─────────────────────────────
        if snapshot_fn:
            add(self._rule_spread_persistence(token, agg, snapshot_fn))

        # ── 11. 多空双杀（新增）─────────────────────────
        add(self._rule_dual_liquidation(token, agg))

        # ── 12. 定点清算（新增）─────────────────────────
        add(self._rule_targeted_liquidation(token, agg))

        # ── 13. 流动性猎杀/插针（新增）──────────────────
        add(self._rule_wick_hunt(token, agg))

        # ── 14. 有序出货 ─────────────────────────────────
        if snapshot_fn:
            add(self._rule_orderly_dump(token, agg, snapshot_fn))

        # ── 阶段判断 ─────────────────────────────────────
        phase = self._phase(token, agg, triggered, snapshot_fn)

        # ── 级别 ─────────────────────────────────────────
        level = ("HIGH"   if score >= 6
                 else "MEDIUM" if score >= 3
                 else "WATCH"  if score >= 1
                 else "NORMAL")

        return {
            "token":     token,
            "score":     score,
            "level":     level,
            "phase":     phase,
            "triggered": triggered,
            "agg":       agg,
            "ts":        int(time.time()),
        }

    # ──────────────────────────────────────────────────────
    # 规则 1：跨所合约价差
    # ──────────────────────────────────────────────────────

    def _rule_futures_spread(self, token: str, agg: dict) -> list:
        results  = []
        spread   = agg.get("max_futures_spread", 0)
        outlier  = agg.get("futures_outlier")
        devs     = agg.get("futures_deviations", {})

        # 必须是单所偏离（非全所同步）
        if outlier:
            other = [abs(v) for ex, v in devs.items() if ex != outlier]
            outlier_dev = abs(devs.get(outlier, 0))
            if other and outlier_dev < statistics.mean(other) * 2:
                return []  # 全所同步移动，是正常行情

        if   spread > 0.03:
            results.append({"rule": "futures_spread_L1", "level": "L1", "score": 3,
                "detail": f"跨所合约价差{spread*100:.2f}% 极度异常，异常方:{outlier}",
                "exchange": outlier})
        elif spread > 0.005:
            results.append({"rule": "futures_spread_L2", "level": "L2", "score": 2,
                "detail": f"跨所合约价差{spread*100:.2f}% 明显异常，异常方:{outlier}",
                "exchange": outlier})
        elif spread > 0.003:
            results.append({"rule": "futures_spread_L3", "level": "L3", "score": 1,
                "detail": f"跨所合约价差{spread*100:.2f}% 进入观察，异常方:{outlier}",
                "exchange": outlier})
        return results

    # ──────────────────────────────────────────────────────
    # 规则 2：现货-合约基差（新增）
    # ──────────────────────────────────────────────────────

    def _rule_basis(self, token: str, agg: dict) -> list:
        results     = []
        basis       = agg.get("basis", {})
        max_basis   = agg.get("max_basis", 0)
        max_basis_ex= agg.get("max_basis_ex")
        spot_avg    = agg.get("spot_avg", 0)

        if not basis or spot_avg == 0:
            return []

        for ex, b in basis.items():
            ab = abs(b)
            direction = "合约溢价" if b > 0 else "合约折价"

            if   ab > BASIS_THRESHOLD["L1"]:   # > 5%
                results.append({"rule": "basis_L1", "level": "L1", "score": 3,
                    "detail": f"{ex} {direction}{ab*100:.2f}%，合约严重脱离现货",
                    "exchange": ex})
            elif ab > BASIS_THRESHOLD["L2"]:   # > 1%
                results.append({"rule": "basis_L2", "level": "L2", "score": 2,
                    "detail": f"{ex} {direction}{ab*100:.2f}%，合约偏离现货",
                    "exchange": ex})
            elif ab > BASIS_THRESHOLD["L3"]:   # > 0.3%
                results.append({"rule": "basis_L3", "level": "L3", "score": 1,
                    "detail": f"{ex} {direction}{ab*100:.2f}%，进入观察",
                    "exchange": ex})

        # 基差 + OI 集中 + 价差持续 = 合约端单独操纵
        oi_shares = agg.get("oi_shares", {})
        spread    = agg.get("max_futures_spread", 0)
        if (max_basis > 0.02
                and max(oi_shares.values(), default=0) > 0.40
                and spread > 0.003):
            results.append({"rule": "basis_manipulation", "level": "L1", "score": 3,
                "detail": (f"基差{max_basis*100:.2f}%+OI集中+价差持续，"
                           f"确认合约端单独拉盘，主场:{max_basis_ex}"),
                "exchange": max_basis_ex})

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

            # 4h 变化率（需要快照）
            if snapshot_fn:
                snaps = snapshot_fn(token, ex, limit=17)
                if len(snaps) >= 16:
                    old = snaps[-1].get("oi_usd", 0) or snaps[-1].get("oi", 0)
                    cur = ois[ex]
                    if old > 0:
                        change = (cur - old) / old
                        if   change > 0.50:
                            results.append({"rule": "oi_change_L1", "level": "L1", "score": 2,
                                "detail": f"{ex} OI 4h暴增{change*100:.1f}%", "exchange": ex})
                        elif change > 0.20:
                            results.append({"rule": "oi_change_L2", "level": "L2", "score": 1,
                                "detail": f"{ex} OI 4h增加{change*100:.1f}%", "exchange": ex})

            # OI 集中度
            share = shares.get(ex, 0)
            if   share > 0.60:
                results.append({"rule": "oi_concentration_L1", "level": "L1", "score": 2,
                    "detail": f"{ex} OI占比{share*100:.1f}% 极度集中", "exchange": ex})
            elif share > 0.45:
                results.append({"rule": "oi_concentration_L2", "level": "L2", "score": 1,
                    "detail": f"{ex} OI占比{share*100:.1f}% 明显集中", "exchange": ex})

        # OI 与价格背离
        if snapshot_fn:
            snaps = snapshot_fn(token, "binance", limit=17)
            if len(snaps) >= 16:
                old_total = snaps[-1].get("total_oi", 0)
                ticker    = agg["raw"].get("binance", {}).get(
                    "futures", {}).get("ticker_24h", {}) or {}
                pc = ticker.get("change_pct_24h", 0) or 0
                if old_total > 0:
                    oi_ch = (total_oi - old_total) / old_total
                    if oi_ch > 0.15 and abs(pc) < 0.01:
                        results.append({"rule": "oi_diverge_accum", "level": "L2", "score": 1,
                            "detail": f"OI堆积{oi_ch*100:.1f}%但价格未动，疑似建仓"})
                    elif oi_ch < -0.15 and pc > 0.03:
                        results.append({"rule": "oi_diverge_dump", "level": "L2", "score": 1,
                            "detail": f"OI下降{abs(oi_ch)*100:.1f}%但价格上涨，疑似出货"})

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
            ab = abs(fr)
            dev = devs.get(ex, 0)

            # 绝对值
            if   ab > 0.001:
                results.append({"rule": "funding_abs_L1", "level": "L1", "score": 2,
                    "detail": f"{ex} 资金费率{fr*100:.4f}% 极端", "exchange": ex})
            elif ab > 0.0005:
                results.append({"rule": "funding_abs_L2", "level": "L2", "score": 1,
                    "detail": f"{ex} 资金费率{fr*100:.4f}% 明显异常", "exchange": ex})

            # 跨所背离
            if   abs(dev) > 0.0008:
                results.append({"rule": "funding_dev_L1", "level": "L1", "score": 2,
                    "detail": f"{ex} 资金费率偏离均值{dev*100:.4f}%", "exchange": ex})
            elif abs(dev) > 0.0004:
                results.append({"rule": "funding_dev_L2", "level": "L2", "score": 1,
                    "detail": f"{ex} 资金费率偏离均值{dev*100:.4f}%", "exchange": ex})

            # 持续性
            raw_fr = (agg["raw"].get(ex, {}).get("futures", {})
                      .get("funding", {}) or {})
            neg = raw_fr.get("negative_periods", 0)
            if   neg >= 5:
                results.append({"rule": "funding_persist_L1", "level": "L1", "score": 2,
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

            # 失衡度绝对值
            if   imb > 0.7:
                results.append({"rule": "imbalance_L1", "level": "L1", "score": 2,
                    "detail": f"{ex} 失衡度{imb:.2f} 极度异常", "exchange": ex})
            elif imb > 0.5:
                results.append({"rule": "imbalance_L2", "level": "L2", "score": 1,
                    "detail": f"{ex} 失衡度{imb:.2f} 明显异常", "exchange": ex})
            elif imb > 0.4:
                results.append({"rule": "imbalance_L3", "level": "L3", "score": 1,
                    "detail": f"{ex} 失衡度{imb:.2f} 进入观察", "exchange": ex})

            # 失衡持续时间（需要快照）
            if snapshot_fn:
                sustained = _sustained_count(
                    snapshot_fn, token, ex, "imbalance", 0.4, "above", 12)
                if   sustained >= 6:
                    results.append({"rule": "imbalance_persist_L1", "level": "L1", "score": 1,
                        "detail": f"{ex} 失衡度>0.4 持续{sustained*15}分钟", "exchange": ex})
                elif sustained >= 4:
                    results.append({"rule": "imbalance_persist_L2", "level": "L2", "score": 1,
                        "detail": f"{ex} 失衡度>0.4 持续{sustained*15}分钟", "exchange": ex})

            # 卖方深度变化
            if snapshot_fn:
                ask_change = _ask_depth_change(snapshot_fn, token, ex, 16)
                if ask_change is not None:
                    if   ask_change < -0.30:
                        results.append({"rule": "ask_depth_L1", "level": "L1", "score": 2,
                            "detail": f"{ex} 卖方深度4h下降{abs(ask_change)*100:.1f}%",
                            "exchange": ex})
                    elif ask_change < -0.15:
                        results.append({"rule": "ask_depth_L2", "level": "L2", "score": 1,
                            "detail": f"{ex} 卖方深度4h下降{abs(ask_change)*100:.1f}%",
                            "exchange": ex})

            # 异常大单 & Spoofing
            dep = depths.get(ex, {})
            if dep:
                for lb in dep.get("large_bids", []):
                    r = lb.get("ratio", 0)
                    if   r > 10:
                        results.append({"rule": "bid_wall_L1", "level": "L1", "score": 2,
                            "detail": f"{ex} 超大买单${lb['qty_usd']:,.0f}(均值{r:.1f}倍)@{lb['price']}",
                            "exchange": ex})
                    elif r > 5:
                        results.append({"rule": "bid_wall_L2", "level": "L2", "score": 1,
                            "detail": f"{ex} 大买单${lb['qty_usd']:,.0f}(均值{r:.1f}倍)@{lb['price']}",
                            "exchange": ex})

                # Spoofing：大单消失
                if snapshot_fn:
                    prev = snapshot_fn(token, ex, limit=1)
                    if prev:
                        prev_prices = {lb["price"] for lb in prev[0].get("large_bids", [])}
                        cur_prices  = {lb["price"] for lb in dep.get("large_bids", [])}
                        disappeared = prev_prices - cur_prices
                        if disappeared:
                            results.append({"rule": "spoofing_L1", "level": "L1", "score": 2,
                                "detail": f"{ex} 大买单消失(Spoofing)@{disappeared}",
                                "exchange": ex})

        return results

    # ──────────────────────────────────────────────────────
    # 规则 6：TWAP 建仓
    # ──────────────────────────────────────────────────────

    def _rule_twap(self, token: str, agg: dict, snapshot_fn) -> list:
        results = []

        # Bid/Ask 比值单调爬升
        trend = _field_trend(snapshot_fn, token, "binance", "imbalance", 10)
        if (trend["monotonic_rising"]
                and trend["change_pct"] > 0.03
                and len(trend["values"]) >= 8):
            results.append({"rule": "twap_creep", "level": "L1", "score": 2,
                "detail": (f"失衡度连续{len(trend['values'])}次单向爬升"
                           f"(累计+{trend['change_pct']*100:.1f}%) TWAP指纹")})

        # 卖方深度单调消失
        for ex in EXCHANGES:
            t = _field_trend(snapshot_fn, token, ex, "ask_depth_usd", 8)
            if (t["monotonic_falling"]
                    and t["change_pct"] < -0.08
                    and len(t["values"]) >= 6):
                results.append({"rule": "twap_ask_drain", "level": "L1", "score": 2,
                    "detail": f"{ex} 卖方深度连续{len(t['values'])}次下降(累计{t['change_pct']*100:.1f}%)",
                    "exchange": ex})

        # 持续买方 Taker 压力（价格未大幅变动）
        tr = (agg["raw"].get("binance", {}).get("futures", {})
              .get("taker_ratio") or {})
        hist = tr.get("history", [])
        ticker = (agg["raw"].get("binance", {}).get("futures", {})
                  .get("ticker_24h") or {})
        pc = ticker.get("change_pct_24h", 0) or 0
        if len(hist) >= 8:
            buy_dom = sum(1 for r in hist[-8:] if r > 1.0)
            if buy_dom >= 7 and abs(pc) < 0.02:
                results.append({"rule": "twap_buy_pressure", "level": "L2", "score": 2,
                    "detail": f"Taker买方连续{buy_dom}期主导但价格未大动，疑似TWAP吸筹"})

        # 小单高频重复
        trades = (agg["raw"].get("binance", {}).get("futures", {})
                  .get("agg_trades") or [])
        if len(trades) >= 50:
            buys = [t for t in trades if t.get("is_buyer")]
            if len(buys) >= 20:
                amts = [t["qty_usd"] for t in buys]
                avg  = statistics.mean(amts)
                small = [t for t in buys if t["qty_usd"] < avg * 0.3]
                if len(small) >= 15:
                    ts_sorted = sorted(t["ts"] for t in small)
                    intervals = [ts_sorted[i+1] - ts_sorted[i]
                                 for i in range(len(ts_sorted)-1)]
                    if intervals:
                        mean_iv = statistics.mean(intervals)
                        std_iv  = statistics.stdev(intervals) if len(intervals) > 1 else 0
                        cv = std_iv / mean_iv if mean_iv > 0 else 1
                        if len(small)/len(buys) > 0.6 and cv < 0.3:
                            results.append({"rule": "twap_small_orders", "level": "L2", "score": 1,
                                "detail": (f"小额买单占{len(small)/len(buys)*100:.0f}%"
                                           f"且时间均匀(CV={cv:.2f})，疑似bot TWAP")})

        return results

    # ──────────────────────────────────────────────────────
    # 规则 7：爆仓代理（Taker 比率）
    # ──────────────────────────────────────────────────────

    def _rule_liquidation(self, token: str, agg: dict) -> list:
        results = []
        tr = (agg["raw"].get("binance", {}).get("futures", {})
              .get("taker_ratio") or {})
        cur = tr.get("current", 1.0)
        if   cur > 2.0:
            results.append({"rule": "liq_proxy_L1", "level": "L1", "score": 2,
                "detail": f"Taker买卖比{cur:.2f}极度买方主导，疑似空头爆仓级联"})
        elif cur > 1.5:
            results.append({"rule": "liq_proxy_L2", "level": "L2", "score": 1,
                "detail": f"Taker买卖比{cur:.2f}买方明显主导"})
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
                    "detail": f"成交量/OI={ratio:.1f}x 极度异常(正常3~8x)，高度疑似刷量"})
            elif ratio > 20:
                results.append({"rule": "wash_L2", "level": "L2", "score": 2,
                    "detail": f"成交量/OI={ratio:.1f}x 疑似刷量"})

        for ex, share in agg.get("oi_shares", {}).items():
            if share > 0.60:
                results.append({"rule": "wash_concentration", "level": "L1", "score": 2,
                    "detail": f"{ex} OI占比{share*100:.1f}%，疑似集中刷量", "exchange": ex})

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
            results.append({"rule": "ls_div_L1", "level": "L1", "score": 2,
                "detail": (f"大户多头({top_ls['current']:.2f})+散户空头"
                           f"({global_ls['current']:.2f})+负费率 经典逼空设置")})
        elif top_long and not retail_long:
            results.append({"rule": "ls_div_L2", "level": "L2", "score": 1,
                "detail": (f"大户多头({top_ls['current']:.2f})"
                           f"vs 散户空头({global_ls['current']:.2f})")})
        return results

    # ──────────────────────────────────────────────────────
    # 规则 10：价差持续时间
    # ──────────────────────────────────────────────────────

    def _rule_spread_persistence(self, token: str, agg: dict, snapshot_fn) -> list:
        results = []
        spread  = agg.get("max_futures_spread", 0)
        outlier = agg.get("futures_outlier")
        if spread <= 0.003:
            return []
        sustained = _sustained_count(
            snapshot_fn, token, "binance", "max_spread", 0.003, "above", 12)
        if   sustained >= 3:
            results.append({"rule": "spread_persist_L1", "level": "L1", "score": 2,
                "detail": (f"跨所价差>0.3%持续{sustained*15}分钟，"
                           f"套利机器人无法抹平，{outlier}流动性被控制"),
                "exchange": outlier})
        elif sustained >= 1:
            results.append({"rule": "spread_persist_L2", "level": "L2", "score": 1,
                "detail": f"跨所价差>0.3%持续{sustained*15}分钟", "exchange": outlier})
        return results

    # ──────────────────────────────────────────────────────
    # 规则 11：多空双杀（新增）
    # ──────────────────────────────────────────────────────

    def _rule_dual_liquidation(self, token: str, agg: dict) -> list:
        """
        多空双杀：OI 不降反升，但多空双方都在爆仓
        做市商在高位做空、低位做多，双向收割
        """
        results = []
        tr = (agg["raw"].get("binance", {}).get("futures", {})
              .get("taker_ratio") or {})
        hist = tr.get("history", [])
        if len(hist) < 4:
            return []

        # 检测 Taker 比率剧烈波动（多空反复切换）
        switches = 0
        for i in range(1, len(hist)):
            if hist[i] > 1.2 and hist[i-1] < 0.8:
                switches += 1
            elif hist[i] < 0.8 and hist[i-1] > 1.2:
                switches += 1

        ticker = (agg["raw"].get("binance", {}).get("futures", {})
                  .get("ticker_24h") or {})
        price_range = 0
        if ticker:
            h = ticker.get("high_24h", 0)
            l = ticker.get("low_24h", 0)
            if l > 0:
                price_range = (h - l) / l

        # 价格剧烈波动 + Taker 多空快速切换 + OI 不降
        oi_change = 0
        # （简化：用当前OI是否高位判断）
        total_oi = agg.get("total_oi", 0)

        if switches >= 3 and price_range > 0.08:
            results.append({"rule": "dual_liq_L1", "level": "L1", "score": 2,
                "detail": (f"Taker多空切换{switches}次+价格振幅{price_range*100:.1f}%，"
                           f"疑似多空双杀")})
        elif switches >= 2 and price_range > 0.05:
            results.append({"rule": "dual_liq_L2", "level": "L2", "score": 1,
                "detail": f"Taker多空切换{switches}次，疑似双向清算"})

        return results

    # ──────────────────────────────────────────────────────
    # 规则 12：定点清算（新增）
    # ──────────────────────────────────────────────────────

    def _rule_targeted_liquidation(self, token: str, agg: dict) -> list:
        """
        定点清算：价格精准触及整数关口后急速反转
        说明做市商在针对特定价位的大量止损单
        """
        results = []

        for ex in ["binance", "bybit"]:
            kl = agg.get("klines", {}).get(ex, [])
            if not kl or len(kl) < 3:
                continue

            for candle in kl[-6:]:   # 检查最近 6 根 K 线
                h = candle.get("high", 0)
                l = candle.get("low", 0)
                c = candle.get("close", 0)
                o = candle.get("open", 0)
                if not (h and l and c and o):
                    continue

                body = abs(c - o)
                if body == 0:
                    continue

                # 检测整数关口（价格的整百分比位）
                for price_level in [h, l]:
                    # 判断是否触及整数关口（末尾两位接近 00 或 50）
                    price_str = f"{price_level:.4f}"
                    last_two  = int(price_str.replace(".", "")[-2:])
                    near_round = last_two < 5 or last_two > 95

                    if near_round:
                        # 检测反转：触及整数关口后收盘方向相反
                        touched_high = (price_level == h and c < o)  # 触及高点后收阴
                        touched_low  = (price_level == l and c > o)  # 触及低点后收阳

                        if touched_high or touched_low:
                            wick = candle.get("upper_wick" if touched_high
                                             else "lower_wick", 0)
                            if wick > body * 1.5:
                                results.append({
                                    "rule":     "targeted_liq_L1",
                                    "level":    "L1",
                                    "score":    2,
                                    "detail":   (f"{ex} 价格触及整数关口"
                                                f"${price_level:.4f}后急速反转，"
                                                f"影线/实体={wick/body:.1f}x，"
                                                f"疑似定点清算"),
                                    "exchange": ex,
                                })
                                break

        return results

    # ──────────────────────────────────────────────────────
    # 规则 13：流动性猎杀/插针（新增）
    # ──────────────────────────────────────────────────────

    def _rule_wick_hunt(self, token: str, agg: dict) -> list:
        """
        插针：单根 K 线影线长度远超实体
        做市商快速拉穿止损密集区后回来
        """
        results = []

        for ex in ["binance", "bybit"]:
            kl = agg.get("klines", {}).get(ex, [])
            if not kl:
                continue

            for candle in kl[-12:]:   # 检查最近 12 根 K 线
                body = candle.get("body", 0)
                if body == 0:
                    body = 0.0001   # 防止除零

                upper = candle.get("upper_wick", 0)
                lower = candle.get("lower_wick", 0)
                max_wick = max(upper, lower)

                if max_wick == 0:
                    continue

                ratio = max_wick / body
                direction = "上影线" if upper > lower else "下影线"

                if   ratio > 5:
                    results.append({"rule": "wick_hunt_L1", "level": "L1", "score": 2,
                        "detail": (f"{ex} {direction}={max_wick:.5f}"
                                  f"(实体{ratio:.1f}倍)，极端插针，"
                                  f"疑似流动性猎杀"),
                        "exchange": ex})
                    break   # 每个交易所只报一次最严重的
                elif ratio > 3:
                    results.append({"rule": "wick_hunt_L2", "level": "L2", "score": 1,
                        "detail": (f"{ex} {direction}={max_wick:.5f}"
                                  f"(实体{ratio:.1f}倍)，疑似插针"),
                        "exchange": ex})
                    break

        return results

    # ──────────────────────────────────────────────────────
    # 规则 14：有序出货
    # ──────────────────────────────────────────────────────

    def _rule_orderly_dump(self, token: str, agg: dict, snapshot_fn) -> list:
        results = []
        pt = _field_trend(snapshot_fn, token, "binance", "price", 8)
        ot = _field_trend(snapshot_fn, token, "binance", "oi_usd", 8)

        if not pt["values"] or not ot["values"]:
            return []

        if (pt["change_pct"] < -0.10
                and ot["change_pct"] < -0.10
                and pt["monotonic_falling"]):
            vals = pt["values"]
            drops = [abs((vals[i]-vals[i-1])/vals[i-1])
                     for i in range(1, len(vals)) if vals[i-1] > 0]
            if drops and max(drops) < 0.02:
                results.append({"rule": "orderly_dump", "level": "L2", "score": 1,
                    "detail": (f"价格均匀下跌(最大单次{max(drops)*100:.1f}%)"
                              f"+OI同步下降，疑似有序出货")})
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
        basis     = agg.get("basis", {})
        max_basis = agg.get("max_basis", 0)

        # 前序阶段（需要外部传入，这里简化）
        prev = None
        if snapshot_fn:
            try:
                from cache.snapshot import get_previous_phase
                prev = get_previous_phase(token)
            except Exception:
                pass

        # 逼空进行中
        squeeze = sum([
            fm < -0.0003,
            cur_taker > 1.5,
            any("liq_proxy_L1" in r for r in rules),
            any("oi_change_L1" in r for r in rules),
            any("ls_div_L1" in r for r in rules),
        ])
        if squeeze >= 3:
            return "🔴 逼空进行中"

        # 逼空收尾（前序是逼空）
        if prev == "🔴 逼空进行中" and fm >= 0 and cur_taker < 1.3:
            return "🟡 逼空收尾"

        # 出货进行中
        dump = sum([
            any("orderly_dump" in r for r in rules),
            any("oi_diverge_dump" in r for r in rules),
            max_basis > 0.02,                               # 合约大幅溢价现货
            any("basis_manipulation" in r for r in rules),
            cur_taker < 0.9,
        ])
        if dump >= 2:
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
            any("twap_ask_drain" in r for r in rules),
            any("twap_buy_pressure" in r for r in rules),
            max_imb > 0.4,
            fm >= -0.0001,
        ])
        if accum >= 3:
            return "🔵 建仓中"

        # 多空双杀
        if any("dual_liq" in r for r in rules):
            return "⚡ 多空双杀"

        # 流动性猎杀
        if any("wick_hunt_L1" in r for r in rules):
            return "🎯 流动性猎杀"

        return "⚫ 信号混合"


# ============================================================
# 快照辅助函数（解耦，通过参数传入）
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


def _ask_depth_change(snapshot_fn, token, exchange, lookback) -> Optional[float]:
    snaps = snapshot_fn(token, exchange, limit=lookback)
    if len(snaps) < 2:
        return None
    newest = snaps[0].get("ask_depth_usd")
    oldest = snaps[-1].get("ask_depth_usd")
    if not newest or not oldest or oldest == 0:
        return None
    return (newest - oldest) / oldest


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
