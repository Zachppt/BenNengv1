# ============================================================
# config.py — 全局配置
# ============================================================

# ── Telegram ──
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN"
TELEGRAM_CHAT_ID   = "YOUR_CHAT_ID"

# ── 扫描设置 ──
SCAN_INTERVAL_MINUTES  = 15     # 正常扫描间隔
HFREQ_INTERVAL_MINUTES = 1      # 高频模式间隔（评分 ≥ 3 的代币）
COLDSTART_SNAPSHOTS    = 16     # 冷启动快照数（16 × 15min = 4h）
SNAPSHOT_RETENTION     = 96     # 保留快照数（96 × 15min = 24h）
HFREQ_SNAPSHOT_RETAIN  = 480    # 高频模式保留快照数（480 × 1min = 8h）

# ── 共享存储路径 ──
SHARED_DIR         = "./shared"
SCAN_RESULT_PATH   = "./shared/scan_result.json"
ALERT_STATE_PATH   = "./shared/alert_state.json"
DB_PATH            = "./shared/snapshots.db"

# ── 交易所合约 API ──
EXCHANGE_FUTURES = {
    "binance": {
        "base":           "https://fapi.binance.com",
        "book_ticker":    "/fapi/v1/ticker/bookTicker",
        "depth":          "/fapi/v1/depth",
        "oi":             "/fapi/v1/openInterest",
        "oi_hist":        "/fapi/v1/openInterestHist",
        "funding":        "/fapi/v1/fundingRate",
        "ticker_24h":     "/fapi/v1/ticker/24hr",
        "ticker_price":   "/fapi/v1/ticker/price",
        "agg_trades":     "/fapi/v1/aggTrades",
        "klines":         "/fapi/v1/klines",
        "taker_ratio":    "/futures/data/takerlongshortRatio",
        "top_ls_ratio":   "/futures/data/topLongShortPositionRatio",
        "global_ls":      "/futures/data/globalLongShortAccountRatio",
    },
    "okx": {
        "base":    "https://www.okx.com",
        "ticker":  "/api/v5/market/ticker",
        "books":   "/api/v5/market/books",
        "oi":      "/api/v5/public/open-interest",
        "funding": "/api/v5/public/funding-rate",
        "trades":  "/api/v5/market/trades",
    },
    "bybit": {
        "base":     "https://api.bybit.com",
        "tickers":  "/v5/market/tickers",
        "orderbook":"/v5/market/orderbook",
        "oi":       "/v5/market/open-interest",
        "funding":  "/v5/market/funding/history",
        "trades":   "/v5/market/recent-trade",
        "ls_ratio": "/v5/market/account-ratio",
    },
    "bitget": {
        "base":    "https://api.bitget.com",
        "ticker":  "/api/mix/v1/market/ticker",
        "depth":   "/api/mix/v1/market/depth",
        "oi":      "/api/mix/v1/market/open-interest",
        "funding": "/api/mix/v1/market/current-fundRate",
        "fills":   "/api/mix/v1/market/fills",
    },
}

# ── 交易所现货 API（新增）──
EXCHANGE_SPOT = {
    "binance": {
        "base":        "https://api.binance.com",
        "price":       "/api/v3/ticker/price",
        "depth":       "/api/v3/depth",
        "ticker_24h":  "/api/v3/ticker/24hr",
    },
    "okx": {
        "base":   "https://www.okx.com",
        "ticker": "/api/v5/market/ticker",      # instId={TOKEN}-USDT (非SWAP)
    },
    "bybit": {
        "base":    "https://api.bybit.com",
        "tickers": "/v5/market/tickers",         # category=spot
    },
    "bitget": {
        "base":   "https://api.bitget.com",
        "ticker": "/api/spot/v1/market/ticker",  # symbol={TOKEN}USDT_SPBL
    },
}

# ── 第一层过滤阈值 ──
FILTER = {
    "price_change_4h": 0.05,   # 4h 价格变动 > 5%
    "spread_pct":      0.003,  # 跨所合约价差 > 0.3%
    "basis_pct":       0.005,  # 现货-合约基差 > 0.5%
    "oi_change_4h":    0.10,   # 单所 OI 4h 变动 > 10%
    "imbalance":       0.40,   # 失衡度 > 0.4
}

# ── 告警阈值 ──
ALERT_THRESHOLD = {
    "HIGH":   6,
    "MEDIUM": 3,
    "WATCH":  1,
}

# ── 推送去重（分钟）──
DEDUP_MINUTES = {
    "HIGH":   30,
    "MEDIUM": 60,
}

# ── 噪音过滤 ──
NOISE_THRESHOLD = 20    # 超过 20 个代币触发同一规则 → 本轮静默

# ── 爆仓最小金额 ──
MIN_LIQUIDATION_USD = 100_000

# ── 基差阈值（现货-合约）──
BASIS_THRESHOLD = {
    "L1": 0.05,   # > 5%
    "L2": 0.01,   # > 1%
    "L3": 0.003,  # > 0.3%
}
