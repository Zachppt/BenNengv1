# ============================================================
# data/fetcher.py — 数据采集层 v2
# 合约数据 + 现货数据（新增）
# ============================================================

import asyncio
import aiohttp
import time
import logging
import statistics
from typing import Optional

logger = logging.getLogger(__name__)


# ────────────────────────────────────────
# 基础请求
# ────────────────────────────────────────

async def fetch(session: aiohttp.ClientSession,
                url: str,
                params: dict = {}) -> Optional[dict]:
    try:
        async with session.get(
            url, params=params,
            timeout=aiohttp.ClientTimeout(total=5)
        ) as r:
            if r.status == 200:
                return await r.json()
            logger.warning(f"HTTP {r.status}: {url}")
            return None
    except Exception as e:
        logger.warning(f"Fetch failed {url}: {e}")
        return None


# ============================================================
# BINANCE — 合约
# ============================================================

class BinanceFuturesFetcher:
    BASE = "https://fapi.binance.com"
    DATA = "https://fapi.binance.com"

    async def all_prices(self, session) -> dict:
        """第一层过滤：批量拉取所有合约价格"""
        data = await fetch(session, f"{self.BASE}/fapi/v1/ticker/price")
        if not data:
            return {}
        return {item["symbol"]: float(item["price"]) for item in data}

    async def book_ticker(self, session, symbol: str) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        if not data:
            return None
        bid = float(data["bidPrice"])
        ask = float(data["askPrice"])
        bid_qty = float(data["bidQty"])
        ask_qty = float(data["askQty"])
        total = bid_qty + ask_qty
        return {
            "bid":       bid,
            "ask":       ask,
            "bid_qty":   bid_qty,
            "ask_qty":   ask_qty,
            "spread":    (ask - bid) / bid if bid > 0 else 0,
            "imbalance": (bid_qty - ask_qty) / total if total > 0 else 0,
            "mid":       (bid + ask) / 2,
            "ts":        data.get("time", int(time.time() * 1000)),
        }

    async def depth(self, session, symbol: str, limit: int = 20) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/fapi/v1/depth", {"symbol": symbol, "limit": limit})
        if not data:
            return None
        bids = [[float(p), float(q)] for p, q in data["bids"]]
        asks = [[float(p), float(q)] for p, q in data["asks"]]
        bid_usd = sum(p * q for p, q in bids)
        ask_usd = sum(p * q for p, q in asks)
        total   = bid_usd + ask_usd
        avg_bid = bid_usd / len(bids) if bids else 0
        avg_ask = ask_usd / len(asks) if asks else 0
        return {
            "bids":          bids,
            "asks":          asks,
            "bid_depth_usd": bid_usd,
            "ask_depth_usd": ask_usd,
            "depth_ratio":   bid_usd / ask_usd if ask_usd > 0 else 999,
            "imbalance":     (bid_usd - ask_usd) / total if total > 0 else 0,
            "avg_bid_level": avg_bid,
            "avg_ask_level": avg_ask,
            "large_bids": [
                {"price": p, "qty_usd": p*q, "ratio": (p*q)/avg_bid}
                for p, q in bids
                if avg_bid > 0 and (p*q) > avg_bid * 3
            ],
            "large_asks": [
                {"price": p, "qty_usd": p*q, "ratio": (p*q)/avg_ask}
                for p, q in asks
                if avg_ask > 0 and (p*q) > avg_ask * 3
            ],
            "ts": data.get("T", int(time.time() * 1000)),
        }

    async def open_interest(self, session, symbol: str) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/fapi/v1/openInterest", {"symbol": symbol})
        if not data:
            return None
        oi  = float(data["openInterest"])
        return {"oi": oi, "ts": data["time"]}

    async def oi_history(self, session, symbol: str,
                         period: str = "1h", limit: int = 48) -> Optional[list]:
        data = await fetch(session,
            f"{self.BASE}/fapi/v1/openInterestHist",
            {"symbol": symbol, "period": period, "limit": limit})
        if not data:
            return None
        return [
            {"oi": float(d["sumOpenInterest"]),
             "oi_usd": float(d["sumOpenInterestValue"]),
             "ts": d["timestamp"]}
            for d in data
        ]

    async def funding_rate(self, session, symbol: str,
                           limit: int = 5) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/fapi/v1/fundingRate",
            {"symbol": symbol, "limit": limit})
        if not data or not isinstance(data, list):
            return None
        rates = [float(d["fundingRate"]) for d in data]
        return {
            "current":          rates[-1] if rates else 0,
            "history":          rates,
            "trend":            ("rising"  if len(rates) >= 2 and rates[-1] > rates[-2]
                                 else "falling" if len(rates) >= 2 and rates[-1] < rates[-2]
                                 else "flat"),
            "negative_periods": sum(1 for r in rates if r < 0),
            "ts":               data[-1]["fundingTime"] if data else 0,
        }

    async def ticker_24h(self, session, symbol: str) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/fapi/v1/ticker/24hr", {"symbol": symbol})
        if not data:
            return None
        return {
            "price":          float(data["lastPrice"]),
            "change_pct_24h": float(data["priceChangePercent"]) / 100,
            "volume_24h":     float(data["volume"]),
            "quote_vol_24h":  float(data["quoteVolume"]),
            "high_24h":       float(data["highPrice"]),
            "low_24h":        float(data["lowPrice"]),
            "ts":             data["time"],
        }

    async def taker_ratio(self, session, symbol: str,
                          period: str = "5m", limit: int = 12) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/futures/data/takerlongshortRatio",
            {"symbol": symbol, "period": period, "limit": limit})
        if not data or not isinstance(data, list):
            return None
        ratios = [float(d["buySellRatio"]) for d in data]
        cur = ratios[-1] if ratios else 1.0
        return {
            "current":      cur,
            "history":      ratios,
            "buy_dominant": cur > 1.1,
            "sell_dominant":cur < 0.9,
        }

    async def top_ls_ratio(self, session, symbol: str,
                           period: str = "5m", limit: int = 12) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/futures/data/topLongShortPositionRatio",
            {"symbol": symbol, "period": period, "limit": limit})
        if not data or not isinstance(data, list):
            return None
        ratios = [float(d["longShortRatio"]) for d in data]
        return {
            "current":  ratios[-1] if ratios else 1.0,
            "history":  ratios,
            "top_long": ratios[-1] > 1.0 if ratios else False,
        }

    async def global_ls_ratio(self, session, symbol: str,
                              period: str = "5m", limit: int = 12) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/futures/data/globalLongShortAccountRatio",
            {"symbol": symbol, "period": period, "limit": limit})
        if not data or not isinstance(data, list):
            return None
        ratios = [float(d["longShortRatio"]) for d in data]
        return {
            "current":     ratios[-1] if ratios else 1.0,
            "history":     ratios,
            "retail_long": ratios[-1] > 1.0 if ratios else False,
        }

    async def agg_trades(self, session, symbol: str,
                         limit: int = 500) -> Optional[list]:
        data = await fetch(session,
            f"{self.BASE}/fapi/v1/aggTrades",
            {"symbol": symbol, "limit": limit})
        if not data:
            return None
        return [
            {
                "price":    float(t["p"]),
                "qty":      float(t["q"]),
                "qty_usd":  float(t["p"]) * float(t["q"]),
                "is_buyer": not t["m"],
                "ts":       t["T"],
            }
            for t in data
        ]

    async def klines(self, session, symbol: str,
                     interval: str = "1h", limit: int = 48) -> Optional[list]:
        data = await fetch(session,
            f"{self.BASE}/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": limit})
        if not data:
            return None
        return [
            {
                "ts":     d[0],
                "open":   float(d[1]),
                "high":   float(d[2]),
                "low":    float(d[3]),
                "close":  float(d[4]),
                "volume": float(d[5]),
                # 影线检测（插针）
                "upper_wick": float(d[2]) - max(float(d[1]), float(d[4])),
                "lower_wick": min(float(d[1]), float(d[4])) - float(d[3]),
                "body":   abs(float(d[4]) - float(d[1])),
            }
            for d in data
        ]


# ============================================================
# BINANCE — 现货（新增）
# ============================================================

class BinanceSpotFetcher:
    BASE = "https://api.binance.com"

    async def price(self, session, symbol: str) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/api/v3/ticker/price", {"symbol": symbol})
        if not data:
            return None
        return {
            "price": float(data["price"]),
            "symbol": data["symbol"],
            "ts": int(time.time() * 1000),
        }

    async def ticker_24h(self, session, symbol: str) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/api/v3/ticker/24hr", {"symbol": symbol})
        if not data:
            return None
        return {
            "price":          float(data["lastPrice"]),
            "change_pct_24h": float(data["priceChangePercent"]) / 100,
            "volume_24h":     float(data["volume"]),
            "quote_vol_24h":  float(data["quoteVolume"]),
            "high_24h":       float(data["highPrice"]),
            "low_24h":        float(data["lowPrice"]),
            "ts":             data["closeTime"],
        }

    async def depth(self, session, symbol: str, limit: int = 10) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/api/v3/depth",
            {"symbol": symbol, "limit": limit})
        if not data:
            return None
        bids = [[float(p), float(q)] for p, q in data["bids"]]
        asks = [[float(p), float(q)] for p, q in data["asks"]]
        bid_usd = sum(p * q for p, q in bids)
        ask_usd = sum(p * q for p, q in asks)
        return {
            "bid_depth_usd": bid_usd,
            "ask_depth_usd": ask_usd,
            "best_bid":      bids[0][0] if bids else 0,
            "best_ask":      asks[0][0] if asks else 0,
        }


# ============================================================
# OKX — 合约
# ============================================================

class OKXFuturesFetcher:
    BASE = "https://www.okx.com"

    def _swap(self, symbol: str) -> str:
        return f"{symbol.replace('USDT','')}-USDT-SWAP"

    async def book_ticker(self, session, symbol: str) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/api/v5/market/ticker", {"instId": self._swap(symbol)})
        if not data or not data.get("data"):
            return None
        d = data["data"][0]
        bid = float(d["bidPx"])
        ask = float(d["askPx"])
        bid_qty = float(d.get("bidSz", 0))
        ask_qty = float(d.get("askSz", 0))
        total = bid_qty + ask_qty
        return {
            "bid":       bid,
            "ask":       ask,
            "bid_qty":   bid_qty,
            "ask_qty":   ask_qty,
            "spread":    (ask - bid) / bid if bid > 0 else 0,
            "imbalance": (bid_qty - ask_qty) / total if total > 0 else 0,
            "mid":       (bid + ask) / 2,
            "ts":        int(d["ts"]),
        }

    async def depth(self, session, symbol: str, limit: int = 20) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/api/v5/market/books",
            {"instId": self._swap(symbol), "sz": limit})
        if not data or not data.get("data"):
            return None
        d = data["data"][0]
        bids = [[float(p), float(q)] for p, q, *_ in d["bids"]]
        asks = [[float(p), float(q)] for p, q, *_ in d["asks"]]
        bid_usd = sum(p * q for p, q in bids)
        ask_usd = sum(p * q for p, q in asks)
        total   = bid_usd + ask_usd
        avg_bid = bid_usd / len(bids) if bids else 0
        return {
            "bid_depth_usd": bid_usd,
            "ask_depth_usd": ask_usd,
            "depth_ratio":   bid_usd / ask_usd if ask_usd > 0 else 999,
            "imbalance":     (bid_usd - ask_usd) / total if total > 0 else 0,
            "large_bids": [
                {"price": p, "qty_usd": p*q, "ratio": (p*q)/avg_bid}
                for p, q in bids
                if avg_bid > 0 and (p*q) > avg_bid * 3
            ],
            "ts": int(d["ts"]),
        }

    async def open_interest(self, session, symbol: str) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/api/v5/public/open-interest",
            {"instId": self._swap(symbol)})
        if not data or not data.get("data"):
            return None
        d = data["data"][0]
        return {"oi": float(d["oi"]), "oi_usd": float(d["oiCcy"]),
                "ts": int(d["ts"])}

    async def funding_rate(self, session, symbol: str) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/api/v5/public/funding-rate",
            {"instId": self._swap(symbol)})
        if not data or not data.get("data"):
            return None
        d = data["data"][0]
        return {"current": float(d["fundingRate"]), "ts": int(d["fundingTime"])}


# ============================================================
# OKX — 现货（新增）
# ============================================================

class OKXSpotFetcher:
    BASE = "https://www.okx.com"

    def _spot(self, symbol: str) -> str:
        return f"{symbol.replace('USDT','')}-USDT"

    async def price(self, session, symbol: str) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/api/v5/market/ticker",
            {"instId": self._spot(symbol)})
        if not data or not data.get("data"):
            return None
        d = data["data"][0]
        last = float(d["last"])
        return {
            "price": last,
            "bid":   float(d["bidPx"]),
            "ask":   float(d["askPx"]),
            "ts":    int(d["ts"]),
        }


# ============================================================
# BYBIT — 合约
# ============================================================

class BybitFuturesFetcher:
    BASE = "https://api.bybit.com"

    async def book_ticker(self, session, symbol: str) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/v5/market/tickers",
            {"category": "linear", "symbol": symbol})
        if not data or not data.get("result", {}).get("list"):
            return None
        d = data["result"]["list"][0]
        bid = float(d["bid1Price"])
        ask = float(d["ask1Price"])
        bid_qty = float(d["bid1Size"])
        ask_qty = float(d["ask1Size"])
        total = bid_qty + ask_qty
        return {
            "bid":       bid,
            "ask":       ask,
            "bid_qty":   bid_qty,
            "ask_qty":   ask_qty,
            "spread":    (ask - bid) / bid if bid > 0 else 0,
            "imbalance": (bid_qty - ask_qty) / total if total > 0 else 0,
            "mid":       (bid + ask) / 2,
            "price":     float(d["lastPrice"]),
            "vol_24h":   float(d["volume24h"]),
            "ts":        int(d["time"]),
        }

    async def depth(self, session, symbol: str, limit: int = 20) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/v5/market/orderbook",
            {"category": "linear", "symbol": symbol, "limit": limit})
        if not data or not data.get("result"):
            return None
        d = data["result"]
        bids = [[float(p), float(q)] for p, q in d["b"]]
        asks = [[float(p), float(q)] for p, q in d["a"]]
        bid_usd = sum(p * q for p, q in bids)
        ask_usd = sum(p * q for p, q in asks)
        total   = bid_usd + ask_usd
        avg_bid = bid_usd / len(bids) if bids else 0
        return {
            "bid_depth_usd": bid_usd,
            "ask_depth_usd": ask_usd,
            "depth_ratio":   bid_usd / ask_usd if ask_usd > 0 else 999,
            "imbalance":     (bid_usd - ask_usd) / total if total > 0 else 0,
            "large_bids": [
                {"price": p, "qty_usd": p*q, "ratio": (p*q)/avg_bid}
                for p, q in bids
                if avg_bid > 0 and (p*q) > avg_bid * 3
            ],
            "ts": int(d["ts"]),
        }

    async def open_interest(self, session, symbol: str) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/v5/market/open-interest",
            {"category": "linear", "symbol": symbol,
             "intervalTime": "5min", "limit": 1})
        if not data or not data.get("result", {}).get("list"):
            return None
        d = data["result"]["list"][0]
        return {"oi": float(d["openInterest"]), "ts": int(d["timestamp"])}

    async def funding_rate(self, session, symbol: str,
                           limit: int = 5) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/v5/market/funding/history",
            {"category": "linear", "symbol": symbol, "limit": limit})
        if not data or not data.get("result", {}).get("list"):
            return None
        rates = [float(d["fundingRate"]) for d in data["result"]["list"]]
        return {
            "current":          rates[0] if rates else 0,
            "history":          rates,
            "negative_periods": sum(1 for r in rates if r < 0),
        }

    async def klines(self, session, symbol: str,
                     interval: str = "60", limit: int = 24) -> Optional[list]:
        """interval: 1,3,5,15,30,60,120,240,360,720,D,W,M"""
        data = await fetch(session,
            f"{self.BASE}/v5/market/kline",
            {"category": "linear", "symbol": symbol,
             "interval": interval, "limit": limit})
        if not data or not data.get("result", {}).get("list"):
            return None
        rows = []
        for d in data["result"]["list"]:
            o, h, l, c = float(d[1]), float(d[2]), float(d[3]), float(d[4])
            body = abs(c - o)
            rows.append({
                "ts":          int(d[0]),
                "open": o, "high": h, "low": l, "close": c,
                "volume":      float(d[5]),
                "upper_wick":  h - max(o, c),
                "lower_wick":  min(o, c) - l,
                "body":        body,
            })
        return rows


# ============================================================
# BYBIT — 现货（新增）
# ============================================================

class BybitSpotFetcher:
    BASE = "https://api.bybit.com"

    async def price(self, session, symbol: str) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/v5/market/tickers",
            {"category": "spot", "symbol": symbol})
        if not data or not data.get("result", {}).get("list"):
            return None
        d = data["result"]["list"][0]
        return {
            "price": float(d["lastPrice"]),
            "bid":   float(d["bid1Price"]),
            "ask":   float(d["ask1Price"]),
            "ts":    int(data["time"]),
        }


# ============================================================
# BITGET — 合约
# ============================================================

class BitgetFuturesFetcher:
    BASE = "https://api.bitget.com"

    def _sym(self, symbol: str) -> str:
        return symbol + "_UMCBL"

    async def book_ticker(self, session, symbol: str) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/api/mix/v1/market/ticker",
            {"symbol": self._sym(symbol)})
        if not data or not data.get("data"):
            return None
        d = data["data"]
        bid = float(d["bestBid"])
        ask = float(d["bestAsk"])
        bid_qty = float(d.get("bestBidSize", 0))
        ask_qty = float(d.get("bestAskSize", 0))
        total = bid_qty + ask_qty
        return {
            "bid":       bid,
            "ask":       ask,
            "bid_qty":   bid_qty,
            "ask_qty":   ask_qty,
            "spread":    (ask - bid) / bid if bid > 0 else 0,
            "imbalance": (bid_qty - ask_qty) / total if total > 0 else 0,
            "mid":       (bid + ask) / 2,
            "price":     float(d["last"]),
            "ts":        int(d["timestamp"]),
        }

    async def depth(self, session, symbol: str, limit: int = 20) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/api/mix/v1/market/depth",
            {"symbol": self._sym(symbol), "limit": limit})
        if not data or not data.get("data"):
            return None
        d = data["data"]
        bids = [[float(p), float(q)] for p, q in d["bids"]]
        asks = [[float(p), float(q)] for p, q in d["asks"]]
        bid_usd = sum(p * q for p, q in bids)
        ask_usd = sum(p * q for p, q in asks)
        total   = bid_usd + ask_usd
        avg_bid = bid_usd / len(bids) if bids else 0
        return {
            "bid_depth_usd": bid_usd,
            "ask_depth_usd": ask_usd,
            "depth_ratio":   bid_usd / ask_usd if ask_usd > 0 else 999,
            "imbalance":     (bid_usd - ask_usd) / total if total > 0 else 0,
            "large_bids": [
                {"price": p, "qty_usd": p*q, "ratio": (p*q)/avg_bid}
                for p, q in bids
                if avg_bid > 0 and (p*q) > avg_bid * 3
            ],
            "ts": int(d.get("timestamp", time.time() * 1000)),
        }

    async def open_interest(self, session, symbol: str) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/api/mix/v1/market/open-interest",
            {"symbol": self._sym(symbol)})
        if not data or not data.get("data"):
            return None
        d = data["data"]
        return {"oi": float(d.get("amount", 0)),
                "ts": int(d.get("timestamp", time.time() * 1000))}

    async def funding_rate(self, session, symbol: str) -> Optional[dict]:
        data = await fetch(session,
            f"{self.BASE}/api/mix/v1/market/current-fundRate",
            {"symbol": self._sym(symbol)})
        if not data or not data.get("data"):
            return None
        return {"current": float(data["data"].get("fundingRate", 0))}


# ============================================================
# BITGET — 现货（新增）
# ============================================================

class BitgetSpotFetcher:
    BASE = "https://api.bitget.com"

    async def price(self, session, symbol: str) -> Optional[dict]:
        spot_symbol = symbol.replace("USDT", "") + "USDT_SPBL"
        data = await fetch(session,
            f"{self.BASE}/api/spot/v1/market/ticker",
            {"symbol": spot_symbol})
        if not data or not data.get("data"):
            return None
        d = data["data"]
        return {
            "price": float(d.get("close", 0)),
            "bid":   float(d.get("buyOne", 0)),
            "ask":   float(d.get("sellOne", 0)),
            "ts":    int(d.get("ts", time.time() * 1000)),
        }


# ============================================================
# 统一采集入口
# ============================================================

# 全局实例
_bf  = BinanceFuturesFetcher()
_bs  = BinanceSpotFetcher()
_of  = OKXFuturesFetcher()
_os  = OKXSpotFetcher()
_yf  = BybitFuturesFetcher()
_ys  = BybitSpotFetcher()
_gf  = BitgetFuturesFetcher()
_gs  = BitgetSpotFetcher()


async def fetch_all_prices(session) -> dict:
    """第一层过滤：批量拉取 Binance 全量合约价格"""
    return await _bf.all_prices(session)


async def fetch_token_full(session, token: str) -> dict:
    """
    采集单个代币的完整数据（合约 + 现货，4 所并发）
    返回结构化 raw dict
    """
    symbol = token + "USDT"

    results = await asyncio.gather(
        # Binance 合约
        _bf.book_ticker(session, symbol),
        _bf.depth(session, symbol),
        _bf.open_interest(session, symbol),
        _bf.funding_rate(session, symbol),
        _bf.ticker_24h(session, symbol),
        _bf.taker_ratio(session, symbol),
        _bf.top_ls_ratio(session, symbol),
        _bf.global_ls_ratio(session, symbol),
        _bf.agg_trades(session, symbol),
        _bf.klines(session, symbol),
        # Binance 现货
        _bs.price(session, symbol),
        _bs.ticker_24h(session, symbol),
        # OKX 合约
        _of.book_ticker(session, symbol),
        _of.depth(session, symbol),
        _of.open_interest(session, symbol),
        _of.funding_rate(session, symbol),
        # OKX 现货
        _os.price(session, symbol),
        # Bybit 合约
        _yf.book_ticker(session, symbol),
        _yf.depth(session, symbol),
        _yf.open_interest(session, symbol),
        _yf.funding_rate(session, symbol),
        _yf.klines(session, symbol),
        # Bybit 现货
        _ys.price(session, symbol),
        # Bitget 合约
        _gf.book_ticker(session, symbol),
        _gf.depth(session, symbol),
        _gf.open_interest(session, symbol),
        _gf.funding_rate(session, symbol),
        # Bitget 现货
        _gs.price(session, symbol),
        return_exceptions=True,
    )

    def s(v):
        return v if not isinstance(v, Exception) else None

    return {
        "binance": {
            "futures": {
                "book_ticker":    s(results[0]),
                "depth":          s(results[1]),
                "oi":             s(results[2]),
                "funding":        s(results[3]),
                "ticker_24h":     s(results[4]),
                "taker_ratio":    s(results[5]),
                "top_ls_ratio":   s(results[6]),
                "global_ls":      s(results[7]),
                "agg_trades":     s(results[8]),
                "klines":         s(results[9]),
            },
            "spot": {
                "price":          s(results[10]),
                "ticker_24h":     s(results[11]),
            },
        },
        "okx": {
            "futures": {
                "book_ticker":    s(results[12]),
                "depth":          s(results[13]),
                "oi":             s(results[14]),
                "funding":        s(results[15]),
            },
            "spot": {
                "price":          s(results[16]),
            },
        },
        "bybit": {
            "futures": {
                "book_ticker":    s(results[17]),
                "depth":          s(results[18]),
                "oi":             s(results[19]),
                "funding":        s(results[20]),
                "klines":         s(results[21]),
            },
            "spot": {
                "price":          s(results[22]),
            },
        },
        "bitget": {
            "futures": {
                "book_ticker":    s(results[23]),
                "depth":          s(results[24]),
                "oi":             s(results[25]),
                "funding":        s(results[26]),
            },
            "spot": {
                "price":          s(results[27]),
            },
        },
    }


async def fetch_token_realtime(session, token: str) -> dict:
    """
    用户触发 /analyze 时的即时补充拉取
    只拉取 Best Bid/Ask 和现货价格（延迟最敏感的数据）
    """
    symbol = token + "USDT"
    results = await asyncio.gather(
        _bf.book_ticker(session, symbol),
        _bs.price(session, symbol),
        _of.book_ticker(session, symbol),
        _os.price(session, symbol),
        _yf.book_ticker(session, symbol),
        _ys.price(session, symbol),
        _gf.book_ticker(session, symbol),
        _gs.price(session, symbol),
        return_exceptions=True,
    )

    def s(v): return v if not isinstance(v, Exception) else None

    return {
        "binance": {"futures_bt": s(results[0]), "spot": s(results[1])},
        "okx":     {"futures_bt": s(results[2]), "spot": s(results[3])},
        "bybit":   {"futures_bt": s(results[4]), "spot": s(results[5])},
        "bitget":  {"futures_bt": s(results[6]), "spot": s(results[7])},
    }
