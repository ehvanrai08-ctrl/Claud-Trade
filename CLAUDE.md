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
6. **Workflow commit steps must stage the shared artifacts** —
   `trades_ledger.jsonl`, `performance.json`, `capital_weights.json`. Any bot
   that realizes a trade writes the ledger; if a workflow leaves it unstaged,
   `git pull --rebase` refuses ("unstaged changes"), all retries fail, and the
   push dies silently behind `|| true` — the run shows GREEN but the report/
   state never reaches the repo (this ate a week of post-market reports).
7. **SEC EDGAR requires a real contact email in the User-Agent** — a generic
   UA gets 403 and `superinvestor_copy.py` silently builds an empty basket.
8. **Secrets live in GitHub, never in chat.** `ALPACA_API_KEY`,
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
| Trend Basket | `trend_basket.py` | self-loops from 9:30 AM, 2 PM handoff | Same ATR-trailing-stop logic across a basket of high-beta names (NVDA/AMD/AVGO/META/AMZN/GOOGL, TSLA excluded — its own bot owns it). Buys only above the 50-day SMA; collision-safe (defers on held symbols, sells only its own qty) |
| Wheel | `wheel_strategy.py` | every 15 min, market hours | Sell cash-secured puts → covered calls if assigned; close at 50% profit |
| Copy Trader | `copy_trader.py` | hourly, market hours | Mirrors the most profitable active US congressperson — data straight from the **official** House Clerk + Senate eFD disclosure systems (`congress_disclosures.py`; Quiver Quant paywalled 2026-07-08, all free mirrors dead). Politicians ranked by realized performance of their disclosed buys via Alpaca prices |
| Superinvestor Copy | `superinvestor_copy.py` | 10:45 AM, first 5 days of month | Mirrors a basket of **low-turnover** 13F managers (Buffett/Ackman/Akre/Gates/Markel) via SEC EDGAR + OpenFIGI (CUSIP→ticker). Holds the top-8 consensus names, equal-weight, monthly. Deliberately copies *slow* compounders — the 45-day 13F lag is harmless on multi-year holds — and never fast traders like Burry (stale + option-heavy filings). Collision-safe; every buy passes the risk guard. |
| Emerging Growth ⚠ | `emerging_growth.py` | 10:55 AM, first 5 days of month | **EXPERIMENTAL / high-variance.** The tradeable leg of "find early companies that become huge": a diversified basket (never a single bet) of emerging high-growth names from the scout's watchlist, ranked by 6–12mo momentum with a 200d trend gate, top-6 equal-weight, monthly. `backtest_emerging.py` was honest: momentum *selection* added ~0 risk-adjusted value over equal-weight-holding the same universe (the SPY-beating return is survivorship bias); the only real benefit is the trend gate cutting maxDD ~68%→57%. So it's sized SMALL ($4k sleeve) and labeled experimental — drawdown-controlled growth exposure, NOT proven alpha. Collision-safe; risk-guarded. |
| ~~TJR~~ **PAUSED** | `tjr_strategy.py` | cron disabled (manual only) | ICT/SMC day trade on SPY/QQQ: liquidity sweep → BOS → FVG → entry. **Paused 2026-06-26**: `backtest_tjr.py` (622d, in/out-of-sample split) found no edge in any of 8 variants (simple vs full stack, long-only vs long+short, fixed vs trail+BE — all PF<1.1, negative risk-adjusted return). Live bot had taken zero trades (8 stacked confluence gates strangle it). Code kept; re-enable cron to revive. |
| Mean Reversion | `mean_reversion.py` | 10 AM daily | Buy RSI<30 + below lower Bollinger with up-day confirmation; sell on revert |
| ~~ORB~~ **PAUSED** | `orb_strategy.py` | cron disabled (manual only) | Opening Range Breakout on QQQ. **Paused 2026-06-24**: backtests showed no edge unleveraged (every variant PF<1). Code kept; re-enable cron in the workflow to revive. |
| ~~SIP-ORB~~ **PAUSED** | `sip_orb.py` | cron disabled (manual only) | Multi-stock Stocks-in-Play ORB (Zarattini/Barbon/Aziz SSRN 4729284). **Paused 2026-06-24**: tuning sweep showed no config reaches PF>1 unleveraged (paper's edge needs 4× leverage + 1000+ stock universe). Code kept; re-enable cron to revive. |
| TSMOM Sleeve | `tsmom_sleeve.py` | 11:05 AM, first 5 days of month | Multi-asset absolute trend-following (Moskowitz-Ooi-Pedersen, long-only): SPY/TLT/GLD/DBC/UUP each get an independent ensembled-6/9/12mo trend signal; OFF slots park in BIL. The fleet's **defensive diversifier leg** — `backtest_tsmom.py` (24-cell grid) was honest: timing adds ~0 Sharpe over holding the basket untimed (null 1.19), but it **halves maxDD (13%→~8%) in every grid cell** and improved out-of-sample (0.86→1.32, side-stepped 2022). NOT alpha; sized $6k, config from the grid's middle (not best cell). Own-qty collision-safe (shares SPY with rsi2/dual_momentum); risk-guarded. |
| Dual Momentum | `dual_momentum.py` | 10:30 AM, first trading day of month | GEM (Antonacci): hold the stronger of SPY/EFA while equities beat cash (absolute gate), else 100% AGG bonds; ensembled 6–12mo lookbacks; ~1.5 trades/yr |
| Sector Momentum | `sector_momentum.py` | 10:35 AM, first trading day of month | Cross-sectional rotation: hold the top 3 of 11 sector SPDRs by ensembled 9–12mo momentum, equal-weight, monthly. Only strategy to survive `backtest_research.py` vs SPY buy-and-hold (Sharpe 1.03 vs 0.88, maxDD 18% vs 34%, robust across the lookback×top_n grid). Risk-adjusted/diversification leg, not a guaranteed index-beater. |
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

## The agent loop — discovery → backtest → optimize

Continuous automation for finding and validating new strategies. Runs weekly
(Mondays 3 AM UTC) via `.github/workflows/agent_loop.yml`.

**Three-agent pipeline:**

1. **`strategy_discovery.py`** — Scans SSRN, Quantpedia, Twitter, Reddit for
   8–12 novel ideas. Filters by liquidity (SPY/QQQ/sector SPDRs/bonds) and
   frequency (daily/monthly). Excludes duplicates of the 10 live strategies.
   Output: natural-language descriptions ready for backtesting.

2. **`backtest_generator.py`** — Takes a strategy description, calls Claude to
   generate working backtest code, validates it (py_compile), runs on Alpaca
   data (2016-2026), extracts metrics (Sharpe, PF, maxDD, CAGR). Verdict:
   PASS (PF>1.0, Sharpe>0.5) | MAYBE (profitable, low robustness) | FAIL.

3. **`parameter_optimizer.py`** — Tunes a live strategy's parameters by sweeping
   ranges, backtesting each combo, recommending the best. Detects when retuning
   would improve Sharpe >5%, suggests "RETUNE" vs "HOLD_CURRENT". Useful when
   a bot's performance drifts or market regime shifts.

**Orchestration: `agent_loop.py`** — Runs all three sequentially, caches
results (skips re-testing), produces weekly markdown report to `reports/`.

**Why this matters:** Research→deploy gap is now closed. Instead of manual
sweeps every month, candidates surface automatically, backtest on real data,
and best performers get continuously tuned. The three agents form a closed
feedback loop.

---

## The project optimizer — continuous code/project improvement

`project_optimizer.py` is the always-on "make the whole project better" agent
(where post_market works on *trades* and the agent loop works on *strategies*,
this works on the *codebase itself*). Runs 3×/week (Tue/Thu/Sat 6 AM UTC) via
`.github/workflows/project_optimizer.yml`.

Each run:
1. **`survey_codebase()`** — deterministic, no API: line counts, all pyflakes
   warnings, TODO/FIXME markers. Always useful even with no credits.
2. **`brainstorm()`** — sends the survey + editable source to Claude Opus, which
   returns (a) safe mechanical code patches and (b) a prioritized idea backlog.
3. **`apply_patches()`** — applies only the safe patches behind the **same two
   gates** as the post-market bot (py_compile + pyflakes "undefined name"),
   rolling back any failure. Anything touching trading *logic* goes to the
   backlog, never an auto-edit.
4. **`update_backlog()`** — merges new ideas into `IMPROVEMENT_BACKLOG.md`
   (deduped, priority-sorted P1/P2/P3, capped at 40), with a per-run log.

**Self-protection:** it NEVER patches itself or `post_market_analysis.py`
(`PROTECTED` set), and never auto-edits `.yml`/`requirements.txt`
(`NEVER_AUTOPATCH`) — so it can't break its own recoverability or the fleet's
infra. The workflow also has a "verify all bots compile" gate before pushing.
Every change is a git commit → one `git revert` away.

`IMPROVEMENT_BACKLOG.md` is the human-readable queue — skim it for P1 ideas
worth doing by hand.

---

## The emerging-company scout (`emerging_scout.py`)

The "find companies early that become huge" research agent. Runs weekly
(Mondays 3 AM UTC) via `.github/workflows/emerging_scout.yml`. Surfaces a ranked
watchlist in two tiers and writes `reports/emerging_watchlist_YYYY-MM-DD.md`:

- **Public & tradeable** — recent IPOs / small-mid-cap growth, verified tradeable
  on Alpaca and written to `emerging_watchlist.json`, which becomes the live
  `emerging_growth.py` bot's candidate universe.
- **Private / pre-IPO** — notable startups you can't trade yet, surfaced to track
  for when they list.

Discovery only — no trading. The honest framing baked into the prompt and the
report: single-name multibagger picking is mostly luck, so the live bot only ever
trades a diversified, risk-controlled basket of the tradeable tier. The live leg
(`emerging_growth.py`) is **experimental and sized small** — see its strategy-table
row and `backtest_emerging.py` for why (momentum selection showed ~0 edge over
the universe; only the trend gate's drawdown control survived).

---

## State & data files

| File | Purpose |
|---|---|
| `strategy_state.json` | TSLA entry, stop order id, current stop, HWM, trailing flag |
| `wheel_state.json` | Active option contract, stage, total premium, cycles |
| `dca_state.json` | Total invested, buy count, last buy date |
| `copy_trader_state.json` | Tracked politician, copied trades |
| `congress_cache.json` | Parsed House/Senate PTR filings (incremental — only new filings fetched per run), pruned at 120 days |
| `mean_reversion_state.json` | Open entries, closed P&L |
| `orb_state.json` | Day's ORB phase, direction, entry, qty, resting stop order id |
| `dual_momentum_state.json` | Current held asset, last rebalance month, rotation history |
| `sector_momentum_state.json` | Sector rotation: per-symbol qty/entry, last rebalance month, history |
| `trend_basket_state.json` | Trend basket: per-symbol entry/qty/stop/HWM/trailing flag |
| `ibs_state.json` | IBS bot: holding flag, entry price/qty/date for QQQ |
| `rsi2_state.json` | RSI(2) bot: holding flag, entry price/qty/date for SPY |
| `superinvestor_state.json` | Superinvestor copy: per-symbol qty/entry, last rebalance month, last 13F accession per manager, history |
| `emerging_growth_state.json` | Emerging-growth basket: per-symbol qty/entry, last rebalance month, history |
| `tsmom_state.json` | TSMOM sleeve: per-symbol qty/entry, last rebalance month, monthly target history |
| `emerging_watchlist.json` | Emerging-company scout output: tradeable tickers (live-bot universe) + public/private candidate details, updated weekly |
| `trades_ledger.jsonl` | Append-only realized-trade log (via `perf.record_trade()`) |
| `performance.json` | Per-strategy win rate / P&L (via `performance_tracker.py`) |
| `capital_weights.json` | Dynamic per-strategy notional multipliers (0.25×–2×), updated nightly by `capital_allocator.py` |
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
