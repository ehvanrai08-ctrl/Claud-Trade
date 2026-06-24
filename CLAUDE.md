# Claud-Trade — Automated Paper Trading System

A fully automated **paper trading** system (Alpaca paper API) that runs entirely
on **GitHub Actions** — no local machine or Claude session required. Multiple
complementary strategies trade independently, performance is tracked per
strategy, and a nightly bot calls the Claude API to analyze the day and
self-improve the code.

> **Status: paper trading only.** No real money. The goal is to prove the system
> works over a few months, then decide whether to switch to real funds.

---

## ⚠️ Read this first (hard-won gotchas)

These bugs were each hit more than once. Don't reintroduce them:

1. **`BASE_DIR` must be dynamic.** Every script uses
   `BASE_DIR = os.path.dirname(os.path.abspath(__file__))`. Never hardcode
   `/home/user/Claud-Trade` — that path does not exist on GitHub runners, and
   `dotenv_values()` will silently return `{}`, causing `KeyError: 'ALPACA_BASE_URL'`.
2. **The default branch is `claude/charming-edison-b7imkj`.** GitHub only runs
   scheduled (cron) workflows from the **default branch**. All development and
   pushes go here. Every workflow checks out this ref explicitly.
3. **`requirements.txt` must exist** or `actions/setup-python` with `cache: pip`
   fails before any code runs.
4. **Use `stop_limit`, not `stop`, for sell stops.** Plain stop orders get
   wash-trade rejected when ladder buy orders are open. Limit sits ~1% below stop.
5. **Orders can have `qty = None`** (fractional/notional buys like the VOO DCA use
   `notional`). Always fall back to `o.get('notional')` when formatting.
6. **Secrets live in GitHub, never in chat.** `ALPACA_API_KEY`,
   `ALPACA_SECRET_KEY`, `ANTHROPIC_API_KEY` are set under
   Settings → Secrets and variables → Actions. **Never paste an API key into the
   chat.** If one is ever pasted, tell the user to revoke and regenerate it.

---

## Quick orientation for a fresh session

- **"Give me a daily report"** → run `python status.py` for a live snapshot, then
  read the latest file in `reports/`.
- **The system is healthy if** workflows are green in the Actions tab and
  `reports/` has a file for the most recent trading day.
- **To undo bad self-edits** → every change is a git commit; `git revert <sha>`
  or revert to a known-good date. The self-improvement loop cannot edit its own
  engine (see below), so it can never break its way out of recoverability.

---

## Strategies (each = one script + one workflow)

| Strategy | File | Schedule (ET) | What it does |
|---|---|---|---|
| TSLA Trailing Stop | `market_monitor.py` | self-loops from 9:30 AM, 2 PM handoff | Holds TSLA, trails an ATR-based stop up (never down); re-enters after a stop-out |
| Wheel | `wheel_strategy.py` | every 15 min, market hours | Sell cash-secured puts → covered calls if assigned; close at 50% profit |
| Copy Trader | `copy_trader.py` | hourly, market hours | Mirrors the most profitable active US congressperson via Quiver Quant |
| TJR | `tjr_strategy.py` | 9:30–11 AM, high-frequency | ICT/SMC day trade on SPY/QQQ: liquidity sweep → BOS → FVG → entry |
| Mean Reversion | `mean_reversion.py` | 10 AM daily | Buy RSI<30 + below lower Bollinger with up-day confirmation; sell on revert |
| ORB | `orb_strategy.py` | self-loops from 9:35 AM, 2 PM handoff | Opening Range Breakout on QQQ (Zarattini/Aziz): trade the break of the first 5-min bar's direction, resting stop at the opposite OR edge, no profit target, flat at 3:55 PM ET |
| SIP-ORB | `sip_orb.py` | self-loops from 9:35 AM, 2 PM handoff | Multi-stock Stocks-in-Play ORB (Zarattini/Barbon/Aziz SSRN 4729284, Sharpe 2.81): top-10 relative-volume stocks each morning, resting stop-limit entry at OR boundary, 0.10×ATR stop, EOD close |
| Dual Momentum | `dual_momentum.py` | 10:30 AM, first trading day of month | GEM (Antonacci): hold the stronger of SPY/EFA while equities beat cash (absolute gate), else 100% AGG bonds; ensembled 6–12mo lookbacks; ~1.5 trades/yr |
| IBS | `ibs_strategy.py` | 3:50 PM daily | Internal Bar Strength mean reversion on QQQ: buy when IBS=(C−L)/(H−L) < 0.20 (closed near low), sell when IBS > 0.80; holds multi-day. Backtest: 69% win rate, PF 2.10 |
| Connors RSI(2) | `rsi2_strategy.py` | 3:50 PM daily | RSI(2) mean reversion on SPY: buy when RSI(2)<10 AND close>200d SMA, sell when close>5d SMA; holds multi-day. Backtest: 72% win rate, PF 1.38 |
| DCA Index | `dca_index.py` | 10 AM Mondays | Buys $500 of VOO weekly, never sells — the "boring base" |
| Post-Market Analysis | `post_market_analysis.py` | 4:15 PM daily | The self-improvement bot (below) |

**Position-collision safety (IBS, RSI(2)).** These two daily swing bots share
symbols with intraday bots (ORB on QQQ; Dual Momentum / SIP-ORB on SPY). To avoid
one bot's `close_position()` wiping out another's shares in the merged broker
position, they: (1) **defer entry** if a position in their symbol already exists,
(2) on exit **sell exactly their own `entry_qty`** (never `close_position`), and
(3) **reconcile** — if their position vanishes, mark flat and record the trade at
the last price. ORB also stands down on any day QQQ is already held.

Schedules are defined in UTC in `.github/workflows/*.yml` (ET = UTC−4 in summer).

---

## The self-improvement loop (`post_market_analysis.py`)

Runs once per trading day at 4:15 PM ET. Each run:

1. **`local_analysis()`** — deterministic checks from live account data (equity,
   P&L, ranked positions, option-expiry warnings, trailing-stop proximity). Always
   runs, needs no API key. The report is useful even with no Claude credits.
2. **`call_claude()`** — sends the day's data + the three core bot files to
   `claude-sonnet-4-6` for a report and concrete code patches.
3. **`apply_patches()`** — applies `FILE/OLD/NEW` patches behind **two safety
   gates**, rolling back on failure:
   - **Gate 1 — `py_compile`**: rejects syntax errors.
   - **Gate 2 — `pyflakes`**: rejects undefined names / missing imports (these
     compile fine but crash at runtime — e.g. an unimported `timedelta`).
   - Patches are applied **longest-`OLD`-first** so a complete revision beats a
     partial draft.
4. Writes `reports/YYYY-MM-DD.md` and commits.

**Mode: full auto-apply.** The bot commits code changes to `market_monitor.py`,
`wheel_strategy.py`, and `copy_trader.py` automatically. It does **not** commit
`post_market_analysis.py`, so the loop can never corrupt its own engine or safety
gates. The gates guarantee patches won't *crash*; they do **not** verify trading
*logic* — so skim the reports periodically.

**Cost:** ~$0.06–0.09 per daily run on `claude-sonnet-4-6`. $5 of credit lasts
roughly 2.5–4 months. Only this bot uses Anthropic credits; the trading bots
do not.

---

## State & data files

| File | Purpose |
|---|---|
| `strategy_state.json` | TSLA entry, stop order id, current stop, HWM, trailing flag |
| `wheel_state.json` | Active option contract, stage, total premium, cycles |
| `dca_state.json` | Total invested, buy count, last buy date |
| `copy_trader_state.json` | Tracked politician, copied trades |
| `mean_reversion_state.json` | Open entries, closed P&L |
| `orb_state.json` | Day's ORB phase, direction, entry, qty, resting stop order id |
| `dual_momentum_state.json` | Current held asset, last rebalance month, rotation history |
| `ibs_state.json` | IBS bot: holding flag, entry price/qty/date for QQQ |
| `rsi2_state.json` | RSI(2) bot: holding flag, entry price/qty/date for SPY |
| `trades_ledger.jsonl` | Append-only realized-trade log (via `perf.record_trade()`) |
| `performance.json` | Per-strategy win rate / P&L (via `performance_tracker.py`) |
| `reports/YYYY-MM-DD.md` | Daily post-market reports |
| `*.log` | Per-bot run logs (committed back to the repo) |

---

## Environment

- **Alpaca paper API** base URL: `https://paper-api.alpaca.markets/v2`
- `.env` is written by each workflow from GitHub Secrets; locally it holds the
  same three keys. Loaded with `dotenv_values(f"{BASE_DIR}/.env")`.
- Python 3.11. Deps in `requirements.txt`: `requests`, `python-dotenv`,
  `anthropic`, `pyflakes`.

## Conventions

- Commit to `claude/charming-edison-b7imkj`. Use `git push -u origin <branch>`,
  retry network failures with backoff. **Do not open a PR unless asked.**
- Bot commits use `[skip ci]` to avoid triggering workflows on state writes.
- Keep edits in the style of the surrounding code; match its comment density.
