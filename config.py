# ============================================================
# config.py — v3
# 从"异动评分制"升级为"上涨概率制"
# ============================================================

# ── Telegram ──
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN"
TELEGRAM_CHAT_ID   = "YOUR_CHAT_ID"

# ── 扫描设置 ──
SCAN_INTERVAL_MINUTES  = 15
HFREQ_INTERVAL_MINUTES = 1
COLDSTART_SNAPSHOTS    = 16
SNAPSHOT_RETENTION     = 96

# ── 共享存储路径 ──
SHARED_DIR       = "./shared"
SCAN_RESULT_PATH = "./shared/scan_result.json"
ALERT_STATE_PATH = "./shared/alert_state.json"
DB_PATH          = "./shared/snapshots.db"

# ── 交易所合约 API ──
EXCHANGE_FUTURES = {
    "binance": {
        "base":         "https://fapi.binance.com",
        "book_ticker":  "/fapi/v1/ticker/bookTicker",
        "depth":        "/fapi/v1/depth",
        "oi":           "/fapi/v1/openInterest",
        "oi_hist":      "/fapi/v1/openInterestHist",
        "funding":      "/fapi/v1/fundingRate",
        "ticker_24h":   "/fapi/v1/ticker/24hr",
        "ticker_price": "/fapi/v1/ticker/price",
        "agg_trades":   "/fapi/v1/aggTrades",
        "klines":       "/fapi/v1/klines",
        "taker_ratio":  "/futures/data/takerlongshortRatio",
        "top_ls_ratio": "/futures/data/topLongShortPositionRatio",
        "global_ls":    "/futures/data/globalLongShortAccountRatio",
    },
    "okx": {
        "base":    "https://www.okx.com",
        "ticker":  "/api/v5/market/ticker",
        "books":   "/api/v5/market/books",
        "oi":      "/api/v5/public/open-interest",
        "funding": "/api/v5/public/funding-rate",
        "klines":  "/api/v5/market/candles",
    },
    "bybit": {
        "base":      "https://api.bybit.com",
        "tickers":   "/v5/market/tickers",
        "orderbook": "/v5/market/orderbook",
        "oi":        "/v5/market/open-interest",
        "funding":   "/v5/market/funding/history",
        "klines":    "/v5/market/kline",
        "ls_ratio":  "/v5/market/account-ratio",
    },
    "bitget": {
        "base":    "https://api.bitget.com",
        "ticker":  "/api/mix/v1/market/ticker",
        "depth":   "/api/mix/v1/market/depth",
        "oi":      "/api/mix/v1/market/open-interest",
        "funding": "/api/mix/v1/market/current-fundRate",
        "klines":  "/api/mix/v1/market/candles",
    },
}

# ── 交易所现货 API ──
EXCHANGE_SPOT = {
    "binance": {
        "base":  "https://api.binance.com",
        "price": "/api/v3/ticker/price",
        "klines":"/api/v3/klines",
    },
    "okx": {
        "base":   "https://www.okx.com",
        "ticker": "/api/v5/market/ticker",
    },
    "bybit": {
        "base":    "https://api.bybit.com",
        "tickers": "/v5/market/tickers",
    },
    "bitget": {
        "base":   "https://api.bitget.com",
        "ticker": "/api/spot/v1/market/ticker",
    },
}

# ── 第一层过滤阈值 ──
FILTER = {
    "price_change_4h":        0.05,
    "price_change_vs_btc":    0.03,   # 新增：相对BTC超额涨幅
    "spread_pct":             0.003,
    "basis_pct":              0.005,
    "oi_change_4h":           0.10,
    "imbalance":              0.40,
}

# ════════════════════════════════════════════════════════
# 核心：信号权重体系（基于山寨币特性设计）
# 权重 = 该信号对"上涨概率"的贡献百分比
# ════════════════════════════════════════════════════════

SIGNAL_WEIGHTS = {

    # ── 前置信号（建仓阶段，最高权重）──
    # 这些信号出现时价格通常还未启动
    # 做市商最难伪造，是最可靠的预警

    "twap_creep":          12,   # 失衡度单向爬升（TWAP指纹）
    "twap_ask_drain":      12,   # 卖方深度单调消失
    "obv_divergence_pos":  11,   # OBV底背离（最难伪造）
    "obv_breakout":        10,   # OBV突破前高（资金提前流入）
    "oi_change_L1":        10,   # OI大幅积累
    "ls_div_L1":           10,   # 大户多头+散户空头+负费率
    "cmf_turning":          9,   # CMF从负转正（机构开始建仓）
    "twap_buy_pressure":    8,   # 持续买方Taker压力
    "vpvr_above_poc":       8,   # 价格突破成交量POC
    "cmf_positive":         6,   # CMF持续为正

    # ── 中期信号（启动前兆）──
    # 启动即将发生，但可能还有1-4小时

    "channel_breakout":    10,   # 下行通道突破（需量能确认）
    "double_bottom":        9,   # 双底形态（需OBV确认）
    "flag_breakout":        8,   # 旗形突破
    "volume_surge_breakout":8,   # 放量突破前高
    "funding_persist_L1":   7,   # 资金费率持续走负
    "oi_concentration_L1":  6,   # OI高度集中
    "futures_spread_L1":    5,   # 跨所价差开始扩大
    "basis_manipulation":   5,   # 基差操纵

    # ── 确认信号（启动中，低权重）──
    # 信号出现时拉盘已经开始
    # 权重低：用于确认，不作为主要依据

    "imbalance_L1":         4,
    "bid_wall_L1":          4,
    "obv_divergence_neg":  -8,   # OBV顶背离（看空，负权重）
    "double_top":          -6,   # 双顶（看空，负权重）
    "wash_L1":              3,   # 洗盘刷量（真实性存疑）
    "liq_proxy_L1":         2,   # 爆仓（已发生）
    "wick_hunt_L1":         2,   # 插针（已发生）
    "targeted_liq_L1":      2,   # 定点清算（已发生）
    "dual_liq_L1":          2,   # 双杀（已发生）

    # ── 社交媒体（不计入概率）──
    # 通过Agent层辅助，不影响概率计算
}

# ── 概率计算参数 ──
BASE_PROBABILITY  = 0.50    # 基础概率（随机水平）
MAX_SIGNAL_SCORE  = 80      # 理论满分
PROB_CAP          = 0.95    # 上限，永远不说100%
PROB_FLOOR        = 0.40    # 下限（负信号可以降低概率）

# ── 预警阈值 ──
ALERT_PROBABILITY = {
    "HIGH":   0.75,    # > 75% 强预警，立即推送
    "MEDIUM": 0.62,    # > 62% 预警，汇总推送
    "WATCH":  0.55,    # > 55% 关注，静默记录
}

# ── 推送去重 ──
DEDUP_MINUTES = {
    "HIGH":   30,
    "MEDIUM": 60,
}

# ── 相对强度过滤 ──
# 只推送明显超出市场均值的代币
RELATIVE_STRENGTH_THRESHOLD = 1.5   # 必须超过市场均值1.5倍

# ── 噪音过滤 ──
NOISE_THRESHOLD = 40    # 从20提高到40，减少误杀

# ── 基差阈值 ──
BASIS_THRESHOLD = {
    "L1": 0.05,
    "L2": 0.01,
    "L3": 0.003,
}

# ── K线分析参数 ──
KLINE_LOOKBACK      = 48    # 分析最近48根1H K线
KLINE_INTERVAL      = "1h"
VOLUME_SURGE_RATIO  = 1.5   # 放量突破：当前量>均量1.5倍
CHANNEL_MIN_TOUCHES = 4     # 通道至少需要4个接触点
DOUBLE_TOP_TOLERANCE= 0.02  # 双顶价格差距容忍度2%
OBV_LOOKBACK        = 20    # OBV计算周期

# ── 入场区间计算参数 ──
ENTRY_SPOT_BUFFER   = 0.002  # 现货价格上方0.2%作为入场下沿
TARGET_OI_MULTIPLIER= 0.6    # 目标价=当前价×(1+OI涨幅×0.6)
TARGET_MAX_UPSIDE   = 0.30   # 目标价最大上涨幅度30%
STOP_LOSS_PCT       = 0.03   # 止损：POC下方3%

# 本地配置覆盖
try:
    from config_local import *  # noqa
except ImportError:
    pass
