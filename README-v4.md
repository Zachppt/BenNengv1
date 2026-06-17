# crypto-anomaly-scanner

**Crypto Pump & Dump Prediction System — Dual Direction**

A real-time crypto derivatives prediction system built on AethirClaw + OpenClaw Skill framework.

Monitors Binance, OKX, Bybit, and Bitget perpetual futures + spot markets across **both long and short directions**. Uses a multi-signal probability engine to predict upcoming price moves **before** they happen, with automated Telegram alerts including K-line charts, entry zones, and paper trading.

> **v4 upgrade:** Added short-side signals, behavior classification (10 market behavior types), 48h accumulation detection (fixes slow-start tokens like RECALL), daily summary reports, backtest auto-recording, and paper trading.

---

## How It Works

```
Every 15 minutes:
1. Fetch all Binance Futures prices
   → Filter tokens with >5% 4h move vs BTC baseline (excess return filter)
2. For each candidate: fetch full data from 4 exchanges (futures + spot, concurrent)
3. Run 65+ rule engine → compute long probability + short probability separately
4. Resolve direction: if gap < 15% → NEUTRAL (no push); otherwise LONG or SHORT
5. Classify behavior type (10 types) → filter REACTIVE tokens (BTC-driven moves)
6. Apply relative strength filter (only push if prob > 1.5x market average)
7. Apply noise filter (if >40 tokens trigger same rule → market-wide move, silence)
8. Push HIGH alerts (prob ≥ 75%) with K-line chart immediately
9. Batch push MEDIUM alerts (prob 62–75%)
10. Silent when no anomaly — daily summary at 00:00 UTC instead

On /analyze TOKEN:
1. Read cached snapshots from SQLite
2. Fetch real-time bid/ask + spot price (live, ~0.5s)
3. Run K-line technical analysis (OBV, VPVR, CMF, pattern detection)
4. Build structured prompt → call Claude API
5. Push full report + K-line chart
```

---

## Architecture

```
VPS (AethirClaw Container)
│
├── Python Scanner (continuous)
│   ├── data/fetcher.py              fetch futures + spot from 4 exchanges
│   ├── rules/engine.py              65+ rules → long prob + short prob
│   ├── rules/behavior_classifier.py 10 behavior types + MM phase detection
│   ├── rules/market_context.py      BTC correlation, excess return, 48h OI
│   ├── rules/kline_analyzer.py      OBV / VPVR / CMF / pattern recognition
│   ├── alerts/chart_generator.py    K-line PNG chart generation
│   ├── alerts/telegram.py           dual-direction alert formatting
│   ├── backtest/recorder.py         auto-record + 4h/8h/24h evaluation
│   ├── paper_trading/account.py     paper trading account system
│   ├── cache/snapshot.py            SQLite snapshot storage
│   └── main.py                      main scan loop
│
├── shared/                          runtime only, not in repo
│   ├── scan_result.json             latest scan output
│   ├── alert_state.json             mute / watch list
│   └── snapshots.db                 historical snapshots + paper trading data
│
└── OpenClaw Skills (on-demand)
    └── crypto-anomaly-scanner/
        ├── token-analyzer           /analyze TOKEN → LLM deep analysis + chart
        └── alert-manager            /mute /watch /status /history
```

---

## Dual Direction Signal System

### Direction Resolution

```
Long probability:   computed from 65+ long signals
Short probability:  computed from 6 core short signals

Direction rules:
  Long prob − Short prob > 15%  →  push LONG alert
  Short prob − Long prob > 15%  →  push SHORT alert
  Gap < 15%                     →  NEUTRAL, no push (conflicting signals)

This prevents false pushes when both sides have signals.
```

### Long Signal Weights (selected)

```
Pre-launch signals (highest weight, price not yet moved):
  TWAP accumulation fingerprint        +12%
  Ask-side depth monotonic drain       +12%
  OBV positive divergence              +11%
  OBV breakout above previous high     +10%
  Large OI accumulation 4h             +10%
  Top trader long + retail short       +10%
  CMF turning from negative to positive +9%
  Sideways accumulation (new v4)       +10%
  Slow 48h OI accumulation (new v4)    +9%

Mid-stage signals:
  Descending channel breakout          +10%
  Double bottom (OBV confirmed)         +9%
  Funding rate sustained negative       +7%
```

### Short Signal Weights (new in v4)

```
  OI + price both falling              +15%   strongest confirmation
  OBV negative divergence              +12%   momentum exhaustion
  Double top (OBV confirmed)           +12%   reversal pattern
  Short squeeze exhausted              +10%   longs now paying
  CMF turning negative at high         +9%    distribution confirmed
  Basis premium collapsing             +8%    MM has finished exiting
  Taker sell-side dominant (6 periods) +6%
  Funding rate flip to positive        +5%
```

---

## Behavior Classification (new in v4)

The system automatically classifies each token into one of 10 behavior types before computing probability:

| Type | Label | Description | Direction bias |
|------|-------|-------------|----------------|
| REACTIVE | 被动跟随 | Following BTC/market-wide move | Filtered out |
| SQUEEZE | 逼空型 | MM engineering short squeeze | Long |
| PUMP_DUMP | 拉高出货型 | Fast pump then distribution | Context-dependent |
| TWAP_ACCUM | TWAP建仓型 | Slow stealth accumulation | Long |
| STEALTH_ACCUM | 隐秘建仓型 | Early-stage hidden buying | Long (early) |
| DISTRIBUTION | 出货型 | MM actively exiting | Short |
| WASHOUT | 洗盘型 | Shaking out weak hands | Watch |
| STOP_HUNT | 止损猎杀型 | Hunting stop-loss clusters | Watch |
| MOMENTUM_RIDE | 借势拉盘型 | Riding BTC momentum | Long (short-term) |
| UNKNOWN | 信号混合 | Mixed signals | Neutral |

REACTIVE tokens are filtered before probability calculation — BTC-driven moves are not individual opportunities.

---

## K-line Technical Analysis

Replaces Fibonacci (unreliable for low-float manipulated tokens) with indicators that work in maker-controlled markets:

| Indicator | Why it works for altcoins |
|-----------|--------------------------|
| **OBV** | MMs must buy to accumulate — leaves volume trace even when price is suppressed |
| **VPVR** | POC = real cost basis of accumulation; true support/resistance |
| **CMF** | Detects institutional inflow/outflow before price moves |
| **Volume structure** | Real breakouts need real volume; distinguishes genuine moves |

Pattern recognition (indicator-confirmed only):

| Pattern | Required confirmation |
|---------|----------------------|
| Descending channel breakout | Close above band + volume > 1.5x avg + OBV breakout |
| Double bottom | OBV positive divergence + volume shrink on 2nd bottom |
| Double top | OBV negative divergence + volume shrink on 2nd top |
| Flag breakout | Volume surge after shrinking-volume consolidation |

---

## 48h Slow-Start Detection (new in v4)

Fixes the RECALL-type token blind spot: tokens that accumulate slowly over 2–3 days before launching.

```
Triggers when:
  48h OI change > 15%               total accumulation is significant
  AND max single-period change < 5%  but each step is small (TWAP)
  AND monotonically rising           no pullbacks in accumulation

Also detects sideways accumulation:
  Price range < 3% over 4h
  AND ask-side depth declining
  AND OBV rising or CMF positive
```

---

## Alert Format

### Long alert

```
📡 TRIA/USDT  做多概率 78%
🟠 建仓中
🔵 TWAP建仓型 · 建仓期 · 做市商介入72% 🔴

💡 入场建议：✅ 适合布局（尚未启动，前置信号明确）

🎯 前置信号
✅ 失衡度连续8次单向爬升 TWAP建仓指纹   +12%
✅ 大户多头+散户空头+负费率              +10%
✅ OBV突破前高（资金提前布局）           +10%
✅ 48h OI缓慢积累23.4%（单次最大3.1%）  +9%

📊 K线结构
趋势：下降趋势
形态：下行通道突破✅（量比2.1x，OBV突破）
CMF：+0.142（资金流入）

💹 价格对比
  Binance   $0.04333
  Bybit     $0.04591  +5.91% ⚠️
  现货均价  $0.04043
  🔴 基差 Bybit 溢价13.60%

🎯 操作参考
做多区间：$0.04290 ~ $0.04333
目标一：$0.04680（+8.0%）
止损：$0.04161（-3.8%）
预计窗口：1~4小时

[K-line chart attached]
🕐 10:49 UTC
➜ /analyze TRIA 深度分析
```

### Short alert

```
🔻 COAI/USDT  做空概率 82%
🔴 出货确认
🟡 出货型 · 出货期 · 做市商介入85% 🔴

🔻 做空信号
⬇️ OI和价格同步下跌，做市商确认离场
⬇️ OBV顶背离（价格新高但资金未跟）
⬇️ CMF持续为负（-0.18），资金持续流出
⬇️ 资金费率翻正，逼空结束

🎯 操作参考
做空区间：$2.840 ~ $2.870
目标一：$2.620（-8.5%）
目标二：$2.440（-15.3%）
止损：$2.990（+4.2%）
预计窗口：4~12小时

⚠️ 高风险警告
流通量极低（24.9%）
做市商可随时发动逼空，做空被爆仓风险极高
建议：仓位≤总资金5%，严格止损
```

---

## Noise Reduction Changes (v4)

| Before (v3) | After (v4) |
|-------------|------------|
| Push scan summary every round | Silent when no anomaly |
| Only one scan summary per round | Daily report at 00:00 UTC |
| All tokens scored equally | REACTIVE tokens filtered before scoring |
| Noise threshold: 20 tokens | Noise threshold: 40 tokens |
| Fixed probability thresholds | Relative to market average (1.5x) |
| Entry advice missing | Entry advice based on elapsed time + price change |

---

## Paper Trading

Simulated trading with real-time prices. Each Telegram user gets their own account.

```
/paper long TOKEN amount    open long position
/paper short TOKEN amount   open short position
/paper close TOKEN          close position
/paper status               account balance + open positions
/paper history              last 10 trades
/paper leaderboard          top 10 by PnL
```

Starting balance: $10,000 USDT per account. Max position: 20% of account per trade.

---

## Backtest Auto-Recording

Every HIGH alert is automatically recorded. Outcomes evaluated at 4h, 8h, and 24h:

```
WIN    → price moved in predicted direction by > 5%
LOSS   → price moved against prediction by > 3%
NEUTRAL → neither threshold hit within the time window
```

Daily report includes win rates by direction, average PnL, and top tokens.

---

## Manipulation Patterns Detected

| Pattern | Key signals |
|---------|-------------|
| MYX-type short squeeze | Sustained negative funding + OI buildup + short liquidation cascade |
| COAI-type pump & dump | Cross-exchange spread + low float + OI collapse |
| TWAP stealth accumulation | Monotonic imbalance creep + ask depth drain |
| Slow 48h accumulation | OI +15% over 48h with no single spike (RECALL-type) |
| Spoofing | Large bid wall appears then disappears |
| Wash trading | Volume/OI > 30x |
| Cross-exchange mark price manipulation | Small exchange outlier price + Binance OI spike |
| Spot-futures basis manipulation | Contract detached from spot > 2% |
| Dual liquidation | OI stable + both longs and shorts liquidated |
| Distribution (short signal) | OI + price both falling, OBV divergence |

---

## Setup

### Requirements

```bash
pip install aiohttp matplotlib
```

### Configuration

```python
# config_local.py  ← never commit this file
TELEGRAM_BOT_TOKEN = "your_real_token"
TELEGRAM_CHAT_ID   = "your_real_chat_id"
```

### Run

```bash
# Test single scan
python3 main.py --once

# Continuous (recommended)
tmux new -s scanner
python3 run.py
# Ctrl+B then D to detach
```

### Install OpenClaw Skill

```bash
cp -r skills/crypto-anomaly-scanner ~/.openclaw/skills/
openclaw restart
```

---

## Telegram Commands

| Command | Layer | Description |
|---------|-------|-------------|
| `/analyze TOKEN` | Agent (LLM) | Deep analysis + K-line chart |
| `/paper long/short TOKEN amount` | Script | Open paper trade |
| `/paper close TOKEN` | Script | Close paper trade |
| `/paper status` | Script | Account balance + positions |
| `/paper leaderboard` | Script | Top traders |
| `/status` | Script | System status |
| `/history TOKEN` | Agent | Last 24h alert log |
| `/mute TOKEN [1h/6h/24h/7d]` | Script | Silence alerts |
| `/watch TOKEN` | Script | High-frequency scan (1min) |

---

## Project Structure

```
crypto-anomaly-scanner/
├── config.py                      signal weights + probability config
├── main.py                        dual-direction scanner loop
├── run.py                         entry point
├── data/
│   └── fetcher.py                 all exchange API calls (futures + spot)
├── cache/
│   └── snapshot.py                SQLite snapshot storage
├── rules/
│   ├── engine.py                  65+ rules → long + short probability
│   ├── behavior_classifier.py     10 behavior types + MM phase detection
│   ├── market_context.py          BTC correlation, excess return, 48h OI
│   └── kline_analyzer.py          OBV / VPVR / CMF / pattern recognition
├── alerts/
│   ├── telegram.py                dual-direction alert formatting
│   └── chart_generator.py         K-line PNG chart (matplotlib)
├── backtest/
│   └── recorder.py                auto-record alerts + evaluate outcomes
├── paper_trading/
│   └── account.py                 paper trading account system
├── agent/
│   └── analyzer.py                OpenClaw agent layer
└── skills/
    └── crypto-anomaly-scanner/
        ├── SKILL.md
        ├── config.yaml
        └── skills/
            ├── token-analyzer/
            └── alert-manager/
```

---

## Changelog

### v4 (current)
- **Short signals**: 6 core short-side rules (OI+price falling, OBV divergence, double top, squeeze exhausted, CMF negative, basis collapsing)
- **Dual probability**: separate long and short probabilities; push only when gap > 15%
- **Behavior classification**: 10 types; REACTIVE tokens filtered before scoring
- **48h slow-start detection**: catches RECALL-type gradual accumulation
- **Sideways accumulation rule**: low volatility + volume anomaly + OBV rising
- **Daily summary**: replaces per-scan push; silent when no anomaly
- **Entry advice**: "recommended / cautious / avoid" based on elapsed time and price change since first alert
- **Low-float short warning**: mandatory risk warning when float < 30% and direction is SHORT
- **Backtest auto-recording**: every HIGH alert recorded; 4h/8h/24h outcomes evaluated automatically
- **Paper trading**: per-user simulated accounts with leaderboard

### v3
- Probability system (replaced raw score)
- K-line analysis: OBV, VPVR, CMF, pattern recognition
- K-line chart pushed with every HIGH alert
- Relative strength filter (1.5x market average)
- Fibonacci removed (replaced with VPVR + OBV levels)

### v2
- Spot price from all 4 exchanges
- Spot-futures basis calculation
- New patterns: dual liquidation, targeted liquidation, wick hunt
- OpenClaw Skill integration

### v1
- Initial release: cross-exchange spread, OI, funding rate monitoring

---

## Known Limitations

| Limitation | Impact | Planned |
|------------|--------|---------|
| Rule thresholds not calibrated on live data | May need tuning | Ongoing after deployment |
| No cross-chain data | Misses multi-chain manipulation | Phase 2: Debank API |
| BubbleMap wallet clustering | Indirect inference only | Phase 2: BubbleMap API |
| Liquidation absolute amount | Using taker ratio as proxy | Phase 2: Coinglass API |
| Float ratio hardcoded for known tokens | Unknown tokens default to 0.5 | Phase 2: CoinGecko API |

---

## License

MIT
