# Kalshi Pipeline V2

An AI-powered breaking news detector that classifies events against **Kalshi** prediction markets and trades automatically when it finds edge.

```
Breaking News (Twitter / Telegram / RSS)
        ↓ (< 5 seconds)
Match to niche Kalshi markets (low contract volume)
        ↓
Claude Classification: bullish / bearish / neutral + materiality
        ↓
Edge detection + quarter-Kelly sizing
        ↓
Instant execution → SQLite log → calibration tracking
```

This is a port of [brodyautomates/polymarket-pipeline](https://github.com/brodyautomates/polymarket-pipeline) to the Kalshi exchange. The AI/news half (classifier, news streams, edge sizing, logging, calibration, dashboard) is unchanged; only the market data, order execution, and auth layers were rewritten for Kalshi.

---

## What's different from the Polymarket version

The pipeline is layered so only the exchange-facing files changed:

| File | Change |
|---|---|
| `kalshi_auth.py` | **New** — RSA-PSS request signing for the Kalshi API |
| `markets.py` | Fetches markets from the Kalshi REST API |
| `market_watcher.py` | Subscribes to the Kalshi WebSocket `ticker` channel |
| `executor.py` | Places orders via `POST /portfolio/orders` |
| `calibrator.py` / `backtest.py` | Read settlement (`result`) from Kalshi |
| `config.py`, `.env.example`, `setup.sh` | Kalshi creds + hosts |

Everything else — `classifier.py`, `news_stream.py`, `matcher.py`, `edge.py`, `scorer.py`, `logger.py`, `dashboard.py`, `pipeline.py`, `cli.py` — is unchanged from the original.

### Four conceptual differences that matter

1. **Prices are in cents (1–99), not decimals.** Kalshi quotes integer cents; the code divides by 100 on read and multiplies by 100 on order placement. `price/100` = implied probability.
2. **One ticker per market, not a YES/NO token pair.** Direction is expressed with `side: "yes"|"no"` on the order, against a single market `ticker`. The `Market.condition_id` field now holds the Kalshi ticker.
3. **Volume is contract count, not USD.** The niche-market filter (`MIN_VOLUME`/`MAX_VOLUME`) now bounds by number of contracts. Defaults retuned to 50–50,000.
4. **Orders are sized in whole contracts.** `MAX_BET_USD` is converted to a contract count using the per-contract cost (`price_cents/100`).

---

## Setup

```bash
cd kalshi-pipeline
bash setup.sh           # interactive — installs deps, writes .env, verifies
```

Or manually:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # then edit it
python cli.py verify
```

### Getting Kalshi credentials

1. Sign in to Kalshi → account settings → **API Keys** → create a key.
2. You receive a **key ID** (UUID) and download an **RSA private key** (`.pem`).
3. Put the key ID and the path to the `.pem` in `.env`:

```
KALSHI_API_KEY_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
KALSHI_PRIVATE_KEY_PATH=kalshi_key.pem
KALSHI_ENV=demo          # demo = paper trading; prod = real money
```

**Start on `demo`.** The demo endpoint (`demo-api.kalshi.co`) is free paper trading with the same API surface — validate the whole pipeline there before switching `KALSHI_ENV=prod` and `DRY_RUN=false`.

---

## Usage

```bash
python cli.py watch              # V2: real-time event-driven pipeline (dry-run)
python cli.py watch --live       # V2: live trading
python cli.py run                # V1: synchronous RSS-based scan
python cli.py dashboard          # live terminal dashboard
python cli.py backtest           # replay against settled markets
python cli.py niche              # browse niche markets
python cli.py verify             # check keys + connections
```

---

## Safety

- Dry-run mode ON by default; demo environment by default.
- `$25` max single bet, `$100` daily limit, quarter-Kelly sizing.
- Niche filter avoids competing with sophisticated bots on the busiest markets.
- All secrets in `.env`; the `.pem` private key is git-ignored.

---

## Disclaimer

For **educational purposes only**. Not financial advice. Kalshi is a CFTC-regulated, US-only exchange and requires KYC. Prediction-market trading carries real risk — you can lose money. The authors are not responsible for any losses. Use at your own risk.
