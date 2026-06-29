"""
Emerging-Company Scout — research/watchlist agent
=================================================
Surfaces companies EARLY that could become huge, as a ranked watchlist for human
review (no trading). Two tiers:

  • PUBLIC & tradeable — names you can act on today (recent IPOs, small/mid-cap
    high-growth). Verified tradeable on Alpaca and written to a machine-readable
    watchlist (emerging_watchlist.json) that the live bot (emerging_growth.py)
    reads as its candidate universe.
  • PRIVATE / pre-IPO — notable late-stage startups you CANNOT trade yet but
    should track for when/if they list. Surfaced for awareness only.

Honesty: picking individual multibaggers is mostly luck (the project's own
research showed single-name edges rarely survive). This agent's job is DISCOVERY
— widen the funnel and surface candidates with a thesis — not to promise winners.
The live bot only ever trades a diversified, risk-controlled basket of the
tradeable tier, never a concentrated single-name bet.

Runs weekly via .github/workflows/emerging_scout.yml. Writes:
  - reports/emerging_watchlist_YYYY-MM-DD.md   (human-readable, ranked, both tiers)
  - emerging_watchlist.json                    (tradeable tickers for the live bot)
"""

import json
import os
import re
import requests
from datetime import datetime
from dotenv import dotenv_values

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config   = dotenv_values(f"{BASE_DIR}/.env")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TODAY = datetime.utcnow().strftime("%Y-%m-%d")

BASE_URL = config.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
HEADERS  = {
    "APCA-API-KEY-ID":     config.get("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": config.get("ALPACA_SECRET_KEY", ""),
}

REPORTS_DIR    = f"{BASE_DIR}/reports"
WATCHLIST_JSON = f"{BASE_DIR}/emerging_watchlist.json"
MAX_TRADEABLE  = 30   # cap the tradeable universe handed to the live bot


def is_tradeable(symbol):
    try:
        r = requests.get(f"{BASE_URL}/assets/{symbol}", headers=HEADERS, timeout=15)
        a = r.json()
        return (a.get("tradable") and a.get("status") == "active"
                and a.get("asset_class") == "us_equity")
    except Exception:
        return False


def call_claude():
    """Ask Claude to surface emerging companies across both tiers with a thesis."""
    if not ANTHROPIC_KEY:
        return None, "ANTHROPIC_API_KEY not set"

    prompt = f"""You are a growth-equity scout. Surface companies EARLY in their growth that could become much larger, for a watchlist (today is {TODAY}; use your knowledge, and be clear it must be human-verified).

Produce TWO tiers.

=== PUBLIC ===
12–18 PUBLIC, US-listed companies still early in their growth (recent IPOs, small/mid-cap, high revenue growth, expanding TAM) — NOT mega-caps that have already won. For each:
TICKER: <symbol>
NAME: <company>
STAGE: <e.g. "recent IPO 2023" | "small-cap growth">
CONVICTION: <high | medium | speculative>
THESIS: <1 sentence: why it could become much bigger>

=== PRIVATE ===
6–10 notable PRIVATE / pre-IPO companies to TRACK (can't trade yet). For each:
NAME: <company>
SECTOR: <sector>
THESIS: <1 sentence>
IPO_WATCH: <why/when it might list>

Rules: spread across sectors (don't pile into one theme); prefer durable growth over hype; flag clearly speculative names as CONVICTION: speculative. Output ONLY the two sections in the exact format above."""

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": "claude-opus-4-8", "max_tokens": 3000,
              "messages": [{"role": "user", "content": prompt}]},
    )
    if r.ok:
        return r.json()["content"][0]["text"], None
    return None, f"Claude API error: {r.status_code} {r.text[:200]}"


def parse_public(text):
    section = text
    if "=== PUBLIC ===" in text:
        section = text.split("=== PUBLIC ===", 1)[1]
    if "=== PRIVATE ===" in section:
        section = section.split("=== PRIVATE ===", 1)[0]
    out = []
    for block in re.split(r"\nTICKER:\s*", section)[1:]:
        lines = block.strip().split("\n")
        c = {"ticker": lines[0].strip().upper()}
        for ln in lines[1:]:
            for key in ("NAME", "STAGE", "CONVICTION", "THESIS"):
                if ln.strip().upper().startswith(key + ":"):
                    c[key.lower()] = ln.split(":", 1)[1].strip()
        if re.fullmatch(r"[A-Z]{1,5}", c["ticker"]):
            out.append(c)
    return out


def parse_private(text):
    if "=== PRIVATE ===" not in text:
        return []
    section = text.split("=== PRIVATE ===", 1)[1]
    out = []
    for block in re.split(r"\nNAME:\s*", section)[1:]:
        lines = block.strip().split("\n")
        c = {"name": lines[0].strip()}
        for ln in lines[1:]:
            for key in ("SECTOR", "THESIS", "IPO_WATCH"):
                if ln.strip().upper().startswith(key + ":"):
                    c[key.lower()] = ln.split(":", 1)[1].strip()
        out.append(c)
    return out


def write_report(public, private, tradeable):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    L = [f"# Emerging-Company Watchlist — {TODAY}", "",
         "_Discovery only — every name needs human verification. Picking single "
         "multibaggers is mostly luck; the live bot trades a diversified, "
         "risk-controlled basket of the tradeable tier, never one concentrated bet._",
         "", "## Public & tradeable (live-bot universe)", ""]
    if public:
        L.append("| Ticker | Company | Stage | Conviction | Tradeable | Thesis |")
        L.append("|---|---|---|---|---|---|")
        for c in public:
            mark = "✅" if c["ticker"] in tradeable else "—"
            L.append(f"| {c['ticker']} | {c.get('name','?')} | {c.get('stage','?')} "
                     f"| {c.get('conviction','?')} | {mark} | {c.get('thesis','')} |")
    else:
        L.append("_(none surfaced this run)_")

    L += ["", "## Private / pre-IPO (track only — not tradeable)", ""]
    if private:
        for c in private:
            L.append(f"- **{c['name']}** ({c.get('sector','?')}) — {c.get('thesis','')} "
                     f"_IPO watch: {c.get('ipo_watch','?')}_")
    else:
        L.append("_(none surfaced this run)_")

    L += ["", "---", f"_Scout run {TODAY}. {len(tradeable)} tradeable names → "
          f"`emerging_watchlist.json` for the live bot._"]
    path = f"{REPORTS_DIR}/emerging_watchlist_{TODAY}.md"
    with open(path, "w") as f:
        f.write("\n".join(L))
    return path


def run():
    text, err = call_claude()
    if err:
        print(f"[SCOUT] {err}")
        return
    public  = parse_public(text)
    private = parse_private(text)
    print(f"[SCOUT] parsed {len(public)} public, {len(private)} private candidates")

    # Verify tradeable on Alpaca; cap the universe.
    tradeable = []
    for c in public:
        if len(tradeable) >= MAX_TRADEABLE:
            break
        if is_tradeable(c["ticker"]):
            tradeable.append(c["ticker"])
    print(f"[SCOUT] {len(tradeable)} verified tradeable: {tradeable}")

    # Write machine-readable watchlist for the live bot (merge-friendly).
    with open(WATCHLIST_JSON, "w") as f:
        json.dump({"updated": TODAY, "tradeable": tradeable,
                   "public": public, "private": private}, f, indent=2)

    path = write_report(public, private, set(tradeable))
    print(f"[SCOUT] report → {path}")


if __name__ == "__main__":
    run()
