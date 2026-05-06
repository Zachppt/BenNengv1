# crypto-anomaly-scanner

**加密衍生品妖币异动监控系统**

A real-time crypto derivatives manipulation detection system built on AethirClaw + OpenClaw Skill framework.

Monitors Binance, OKX, Bybit, and Bitget perpetual futures + spot markets to detect market maker manipulation patterns, with automated Telegram alerts.

---

## Architecture

```
VPS (AethirClaw Container)
│
├── Python Scanner (continuous)
│   ├── Fetches futures + spot data from 4 exchanges
│   ├── Runs 60+ rule engine (no LLM)
│   ├── Writes results to ./shared/
│   └── Pushes concise alerts to Telegram
│
├── shared/                    ← runtime only, not in repo
│   ├── scan_result.json
│   ├── alert_state.json
│   └── snapshots.db
│
└── OpenClaw Skills (on-demand)
    └── crypto-anomaly-scanner/
        ├── token-analyzer     ← /analyze TOKEN → LLM deep analysis
        └── alert-manager      ← /mute /watch /status /history
```

**Two-layer design:**
- **Script layer** handles all data collection and rule computation — free, fast, no LLM
- **Agent layer** (OpenClaw Skill) handles user commands and LLM deep analysis — only called on demand

---

## Detection Coverage

### Data Sources (all public APIs, no keys required)

| Exchange | Futures Endpoints | Spot Endpoints |
|----------|------------------|----------------|
| Binance  | 9 (OI hist, taker ratio, top L/S, agg trades, klines...) | 2 |
| OKX      | 4 | 1 |
| Bybit    | 5 (incl. klines) | 1 |
| Bitget   | 4 | 1 |

### Rule Engine (60+ rules)

| Category | Rules | Key Signal |
|----------|-------|------------|
| Cross-exchange futures spread | 5 | Spread > 3% on single exchange |
| Spot-futures basis (new) | 3 | Basis > 5%, contract detached from spot |
| OI change | 6 | Single exchange 4h surge > 50% |
| OI concentration | 3 | Single exchange share > 60% |
| Funding rate | 7 | 5 consecutive negative periods / cross-exchange deviation |
| Order book imbalance | 5 | Imbalance > 0.7, sustained 90min |
| Ask depth drain | 3 | 4h decline > 30% |
| Abnormal bid wall / Spoofing | 5 | Single level > 10x average, wall disappears |
| TWAP accumulation | 6 | Monotonic imbalance creep, uniform small orders |
| Liquidation proxy | 2 | Taker ratio > 2.0 |
| Wash trading | 3 | Volume/OI > 30x |
| Top trader vs retail divergence | 2 | Top long + retail short + negative funding |
| Spread persistence | 2 | Spread > 0.3% sustained 45min |
| Dual liquidation (new) | 3 | OI stable + both sides liquidated |
| Targeted liquidation (new) | 2 | Round number touch + sharp reversal |
| Wick hunt / pin bar (new) | 2 | Wick > 5x candle body |
| Orderly distribution | 1 | Uniform price decline + OI drop |

### Manipulation Patterns Detected

- MYX-type short squeeze (slow, consolidation trap)
- COAI-type pump & dump (fast, low float)
- TWAP stealth accumulation
- Spoofing (fake bid wall)
- Wash trading / OI brushing
- Cross-exchange mark price manipulation
- Spot-futures basis manipulation (new)
- Dual liquidation / both-side wipeout (new)
- Targeted liquidation at stop clusters (new)
- Liquidity hunt / wick insertion (new)

---

## Telegram Output

### Script layer — concise alert (auto-push)

```
🚨 TRIA/USDT  Score 18  🔴 Short Squeeze Active
First detected: 90 min ago

💹 Cross-exchange prices
  Binance   $0.04333   baseline
  OKX       $0.04332  -0.02%  ✅
  Bitget    $0.04330  -0.07%  ✅
  Bybit     $0.04591  +5.91%  ⚠️ anomaly
  Spot avg  $0.04043
  ⚠️ Max basis: Bybit contract premium 13.60% — detached from spot

📊 OI distribution
  Bybit    $132.6M  42.2%  +31.4%  🔴 anomaly

💰 Funding rates
  Bybit  -0.0480%  🔴 3 consecutive negative periods

🕐 Data: 10:49:15 UTC · ~15s delay
➜ Reply /analyze TRIA for deep analysis
```

### Agent layer — deep analysis (user-triggered)

Triggered by `/analyze TRIA` or natural language like "分析一下TRIA"

Returns: real-time prices + spot basis + AI phase analysis + risk scores

---

## Setup

### Requirements

```bash
pip install aiohttp
# sqlite3 is Python standard library
```

### Configuration

Edit `config.py`:

```python
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN"   # from @BotFather
TELEGRAM_CHAT_ID   = "YOUR_CHAT_ID"     # from @userinfobot
```

**Never commit real credentials.** Use `config_local.py` for actual values (already in `.gitignore`).

### Run

```bash
# Continuous mode (recommended)
python run.py

# Single scan (for cron job)
python main.py --once

# Background
nohup python run.py > scanner.log 2>&1 &
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
| `/analyze TOKEN` | Agent (LLM) | Deep analysis, ~15s |
| `/status` | Script | System status |
| `/history TOKEN` | Agent | Last 24h anomaly log |
| `/mute TOKEN [1h/6h/24h/7d]` | Script | Silence alerts |
| `/watch TOKEN` | Script | Add to 1-min high-frequency scan |
| `/help` | Script | Show commands |

Natural language also works: "分析一下TRIA", "COAI有没有异动", "现在有什么妖币"

---

## How It Works

```
Every 15 minutes:
1. Fetch all Binance Futures prices (Layer 1 filter)
2. Flag tokens with 4h price move > 5% as candidates
3. For each candidate: fetch full data from 4 exchanges (futures + spot, concurrent)
4. Compute spot-futures basis for all exchanges
5. Run 60+ rule engine (pure Python, no LLM)
6. Apply noise filter (if >20 tokens trigger same rule → market-wide move, silence)
7. Push HIGH alerts (score ≥ 6) immediately
8. Batch push MEDIUM alerts (score 3–5)

On /analyze:
1. Read cached snapshots from SQLite
2. Fetch real-time bid/ask + spot price (live, ~0.5s)
3. Build structured prompt from computed anomaly flags
4. Call Claude API (LLM only processes pre-computed data, not raw numbers)
5. Push full report
```

### Cold Start

TWAP detection requires historical snapshots. System collects data silently for 4 hours (16 snapshots) before activating TWAP rules. All other rules are active from the first scan.

### Anti-false-positive Mechanisms

- Multi-signal convergence: single signal alone does not trigger alert
- Dynamic baselines: thresholds relative to each token's 30-day OI history, not fixed values
- Time-series validation: anomaly must persist across multiple snapshots
- Market-wide noise filter: if >20 tokens trigger the same rule, it's a market move, not manipulation

---

## Known Limitations

| Limitation | Impact | Planned |
|------------|--------|---------|
| No cross-chain data | Misses multi-chain transfer manipulation | Phase 2: Debank API |
| BubbleMap wallet clustering | Only indirect inference | Phase 2: BubbleMap API |
| Liquidation absolute amount | Using taker ratio as proxy | Phase 2: Coinglass API |
| Aster exchange | No public API | Monitor |
| Rule thresholds not calibrated | Requires real-market validation | Ongoing after launch |

---

## Project Structure

```
crypto-anomaly-scanner/
├── config.py                  ← configuration (use config_local.py for real values)
├── main.py                    ← scanner main loop
├── run.py                     ← entry point (scanner + telegram bot)
├── data/
│   └── fetcher.py             ← all exchange API calls (futures + spot)
├── cache/
│   └── snapshot.py            ← SQLite snapshot storage
├── rules/
│   └── engine.py              ← rule engine (60+ rules)
├── alerts/
│   └── telegram.py            ← alert formatting and push
├── agent/
│   └── analyzer.py            ← OpenClaw agent layer (/analyze handler)
└── skills/
    └── crypto-anomaly-scanner/
        ├── SKILL.md            ← main skill definition
        ├── config.yaml         ← cron job configuration
        └── skills/
            ├── token-analyzer/ ← deep analysis skill
            └── alert-manager/  ← alert management skill
```

---

## License

MIT
