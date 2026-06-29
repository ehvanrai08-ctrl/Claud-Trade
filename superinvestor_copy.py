"""
Superinvestor Copy Bot — mirrors low-turnover 13F managers (NOT Michael Burry)
==============================================================================
Companion to copy_trader.py (which mirrors congresspeople). This one mirrors a
hand-picked set of *slow*, buy-and-hold institutional managers via their SEC
13F-HR filings. The whole design rests on one rule established when this was
scoped:

    Copy SLOW investors (the 45-day 13F lag is harmless on multi-year holds) —
    NEVER fast ones like Burry, whose 13F is stale by the time you see it and
    whose option-heavy positions the filing doesn't even show correctly.

So the universe is deliberately Buffett-style compounders, not traders.

DATA (all free, no paid API):
  1. SEC EDGAR — the authoritative source. For each manager CIK we read
     data.sec.gov/submissions/CIK##########.json, find the latest 13F-HR, fetch
     its information-table XML, and parse (cusip, value, shares) per holding.
  2. OpenFIGI — maps CUSIP → US ticker (13F reports CUSIPs, not tickers). Free,
     batched, rate-limited; we only map each manager's top holdings.

STRATEGY (monthly — 13Fs only update quarterly, so monthly catches new filings
within ~30 days and most months are no-ops):
  1. For each manager, take the top HOLDINGS_PER_MGR positions by reported value.
  2. Map CUSIP→ticker, keep tradeable US equities.
  3. Score each ticker by CONSENSUS (how many managers hold it) + aggregate
     portfolio weight. Target basket = top TARGET_NAMES, equal-weighted.
  4. Rebalance: sell names that dropped out (own qty only), buy new entrants.

Collision-safe like sector_momentum: tracks its own per-symbol qty, only ever
sells what it bought, reconciles if a position vanishes, and every buy passes the
portfolio risk guard. Capital-weighted by get_weight("superinvestor").
"""

import json
import logging
import os
import time
import xml.etree.ElementTree as ET
import requests
from datetime import datetime, timezone
from dotenv import dotenv_values
from perf import record_trade
from capital_allocator import get_weight
from risk_guard import can_enter

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
config   = dotenv_values(f"{BASE_DIR}/.env")

BASE_URL = config["ALPACA_BASE_URL"]
HEADERS  = {
    "APCA-API-KEY-ID":     config["ALPACA_API_KEY"],
    "APCA-API-SECRET-KEY": config["ALPACA_SECRET_KEY"],
    "Content-Type":        "application/json",
}
DATA_HEADERS = {
    "APCA-API-KEY-ID":     config["ALPACA_API_KEY"],
    "APCA-API-SECRET-KEY": config["ALPACA_SECRET_KEY"],
}

STATE_FILE = f"{BASE_DIR}/superinvestor_state.json"

# SEC EDGAR requires a descriptive User-Agent with a contact (fair-access policy).
# SEC fair-access policy wants a descriptive UA with a contact. Kept generic so
# no personal email is published in a public repo; edit to your own if you prefer.
SEC_HEADERS = {"User-Agent": "Claud-Trade research bot (contact via GitHub repo)",
               "Accept-Encoding": "gzip, deflate"}

# The managers to mirror — chosen for LOW TURNOVER so the 13F lag is harmless.
# CIKs are SEC central-index keys; verify/extend at
# https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany . A wrong/dead CIK
# is skipped gracefully (the bot just gets no holdings for it), never crashes.
MANAGERS = {
    "1067983": "Berkshire Hathaway (Buffett)",
    "1336528": "Pershing Square (Ackman)",
    "1112520": "Akre Capital Management",
    "1166559": "Gates Foundation Trust",
    "1096343": "Markel Group (Gayner)",
}

HOLDINGS_PER_MGR = 12     # top N positions per manager fed into the consensus
TARGET_NAMES     = 8      # size of the final equal-weight basket
ALLOCATION       = 8000   # $ total sleeve → ALLOCATION / TARGET_NAMES per name

logging.basicConfig(
    filename=f"{BASE_DIR}/superinvestor.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger()


# ── Alpaca helpers ──────────────────────────────────────────────────────────--

def market_is_open():
    r = requests.get(f"{BASE_URL}/clock", headers=HEADERS)
    return r.json().get("is_open", False)


def latest_price(symbol):
    r = requests.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest",
                     headers=DATA_HEADERS)
    return float(r.json()["trade"]["p"]) if r.ok else None


def is_tradeable(symbol):
    try:
        r = requests.get(f"{BASE_URL}/assets/{symbol}", headers=HEADERS)
        a = r.json()
        # Alpaca's asset object uses the field "class" (not "asset_class").
        asset_cls = a.get("class") or a.get("asset_class")
        return (a.get("tradable") and a.get("status") == "active"
                and asset_cls == "us_equity")
    except Exception:
        return False


def get_position(symbol):
    r = requests.get(f"{BASE_URL}/positions/{symbol}", headers=HEADERS)
    return r.json() if r.ok else None


def sell_qty(symbol, qty):
    """Sell exactly `qty` shares — never close_position (protects shared positions
    and keeps us selling only what this bot bought)."""
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol": symbol, "qty": str(qty), "side": "sell",
        "type": "market", "time_in_force": "day",
    })
    if r.ok:
        return r.json()
    log.error(f"Sell failed {symbol}: {r.text[:200]}")
    return None


def buy_notional(symbol, notional):
    r = requests.post(f"{BASE_URL}/orders", headers=HEADERS, json={
        "symbol": symbol, "notional": str(round(notional, 2)),
        "side": "buy", "type": "market", "time_in_force": "day",
    })
    if r.ok:
        return r.json()
    log.error(f"Buy failed {symbol}: {r.text[:200]}")
    return None


# ── SEC EDGAR: latest 13F holdings per manager ─────────────────────────────────

def latest_13f(cik):
    """Return (accession, info_table_url) for the manager's most recent 13F-HR,
    or (None, None) if none found / lookup failed."""
    cik10 = cik.zfill(10)
    try:
        r = requests.get(f"https://data.sec.gov/submissions/CIK{cik10}.json",
                         headers=SEC_HEADERS, timeout=20)
        if not r.ok:
            return None, None
        recent = r.json().get("filings", {}).get("recent", {})
        forms  = recent.get("form", [])
        accns  = recent.get("accessionNumber", [])
        for form, accn in zip(forms, accns):
            if form in ("13F-HR", "13F-HR/A"):
                accn_nodash = accn.replace("-", "")
                idx_url = (f"https://www.sec.gov/Archives/edgar/data/"
                           f"{int(cik)}/{accn_nodash}/index.json")
                return accn, idx_url
    except Exception as e:
        log.warning(f"EDGAR submissions lookup failed for CIK {cik}: {e}")
    return None, None


def fetch_info_table_holdings(idx_url):
    """From a filing's index.json, find the information-table XML and parse it
    into a list of {cusip, issuer, value} dicts (value used only for ranking)."""
    try:
        r = requests.get(idx_url, headers=SEC_HEADERS, timeout=20)
        if not r.ok:
            return []
        items = r.json().get("directory", {}).get("item", [])
        base  = idx_url.rsplit("/", 1)[0]

        # The info table is an .xml that is NOT the primary_doc cover. Prefer a
        # name hinting "info"/"table"; else any .xml that isn't primary_doc.xml.
        xmls = [it["name"] for it in items if it.get("name", "").lower().endswith(".xml")]
        candidates = [n for n in xmls if "info" in n.lower() or "table" in n.lower()]
        if not candidates:
            candidates = [n for n in xmls if n.lower() != "primary_doc.xml"]
        if not candidates:
            return []

        xr = requests.get(f"{base}/{candidates[0]}", headers=SEC_HEADERS, timeout=20)
        if not xr.ok:
            return []
        return parse_info_table(xr.content)
    except Exception as e:
        log.warning(f"Info-table fetch failed for {idx_url}: {e}")
        return []


def parse_info_table(xml_bytes):
    """Parse a 13F information-table XML (namespace-agnostic — match on local
    tag names since the namespace URI varies by filing year)."""
    holdings = []
    try:
        root = ET.fromstring(xml_bytes)
    except Exception:
        return holdings

    def local(tag):
        return tag.split("}")[-1]

    for el in root.iter():
        if local(el.tag) != "infoTable":
            continue
        cusip = issuer = None
        value = 0.0
        for child in el.iter():
            t = local(child.tag)
            if t == "cusip" and child.text:
                cusip = child.text.strip().upper()
            elif t == "nameOfIssuer" and child.text:
                issuer = child.text.strip()
            elif t == "value" and child.text:
                try:
                    value = float(child.text.strip())
                except ValueError:
                    value = 0.0
        if cusip:
            holdings.append({"cusip": cusip, "issuer": issuer or "?", "value": value})
    return holdings


# ── OpenFIGI: CUSIP → ticker ───────────────────────────────────────────────────

def cusip_to_tickers(cusips):
    """Map a list of CUSIPs to US tickers via OpenFIGI (free, batched, ≤10/req).
    Returns {cusip: ticker}. Unmapped CUSIPs are simply omitted."""
    out = {}
    url = "https://api.openfigi.com/v3/mapping"
    uniq = list(dict.fromkeys(cusips))   # dedup, preserve order
    for i in range(0, len(uniq), 10):
        batch = uniq[i:i + 10]
        jobs  = [{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in batch]
        try:
            r = requests.post(url, json=jobs,
                              headers={"Content-Type": "application/json"}, timeout=20)
            if r.status_code == 429:
                time.sleep(6)   # rate-limited without a key (25 req/min) — back off
                r = requests.post(url, json=jobs,
                                  headers={"Content-Type": "application/json"}, timeout=20)
            if not r.ok:
                continue
            for cusip, res in zip(batch, r.json()):
                data = res.get("data") or []
                if data and data[0].get("ticker"):
                    out[cusip] = data[0]["ticker"].strip().upper()
        except Exception as e:
            log.warning(f"OpenFIGI batch failed: {e}")
        time.sleep(0.3)   # be polite to the free endpoint
    return out


# ── Build the consensus target basket ──────────────────────────────────────────

def build_target_basket(state):
    """Pull each manager's latest top holdings, map to tickers, and score by
    consensus + aggregate weight. Returns (target_set, scores, filings_seen)."""
    cusip_pool   = []
    per_mgr      = {}     # cik -> [(cusip, weight_within_mgr)]
    filings_seen = {}

    for cik, name in MANAGERS.items():
        accn, idx_url = latest_13f(cik)
        if not accn:
            log.info(f"{name}: no 13F found.")
            continue
        filings_seen[cik] = accn
        holdings = fetch_info_table_holdings(idx_url)
        if not holdings:
            log.info(f"{name}: 13F {accn} had no parseable holdings.")
            continue
        holdings.sort(key=lambda h: h["value"], reverse=True)
        top = holdings[:HOLDINGS_PER_MGR]
        total = sum(h["value"] for h in top) or 1.0
        per_mgr[cik] = [(h["cusip"], h["value"] / total) for h in top]
        cusip_pool.extend(h["cusip"] for h in top)
        log.info(f"{name}: 13F {accn}, {len(holdings)} holdings, top {len(top)} taken.")
        time.sleep(0.2)

    if not cusip_pool:
        return set(), {}, filings_seen

    tickers = cusip_to_tickers(cusip_pool)

    # Score: consensus (managers holding) weighted heavily, plus summed weight.
    scores = {}
    for cik, items in per_mgr.items():
        for cusip, wt in items:
            tk = tickers.get(cusip)
            if not tk:
                continue
            s = scores.setdefault(tk, {"holders": set(), "weight": 0.0})
            s["holders"].add(cik)
            s["weight"] += wt

    ranked = sorted(
        scores.items(),
        key=lambda kv: (len(kv[1]["holders"]), kv[1]["weight"]),
        reverse=True,
    )
    final_scores = {tk: {"holders": len(v["holders"]), "weight": round(v["weight"], 3)}
                    for tk, v in ranked}

    # Take the top names that are actually tradeable on Alpaca.
    target = []
    for tk, _ in ranked:
        if len(target) >= TARGET_NAMES:
            break
        if is_tradeable(tk):
            target.append(tk)
        else:
            log.info(f"Skip {tk} — not tradeable on Alpaca.")
    return set(target), final_scores, filings_seen


# ── State ───────────────────────────────────────────────────────────────────--

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            log.warning("State unreadable — starting flat.")
    return {"holdings": {}, "last_rebalance_month": None,
            "last_filings": {}, "history": []}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Strategy ──────────────────────────────────────────────────────────────────

def run():
    if not market_is_open():
        log.info("Market closed — skipping.")
        print("[SUPER] Market closed")
        return

    state = load_state()
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    if state.get("last_rebalance_month") == this_month:
        log.info(f"Already rebalanced for {this_month} — standing down.")
        print(f"[SUPER] Already rebalanced this month ({this_month})")
        return

    target, scores, filings = build_target_basket(state)
    if not target:
        log.warning("No target basket built (EDGAR/OpenFIGI returned nothing) — skipping.")
        print("[SUPER] No basket built — skipping")
        return

    held = dict(state.get("holdings", {}))
    log.info(f"Target basket: {sorted(target)} | currently held: {sorted(held)}")
    print(f"[SUPER] target {sorted(target)} | held {sorted(held)}")

    # ── Sell names that dropped out of the basket (our own qty only) ───────────
    for sym in list(held):
        if sym in target:
            continue
        pos = get_position(sym)
        if not pos:
            log.warning(f"{sym} in state but no live position — reconciling out.")
            held.pop(sym, None)
            continue
        owned = int(float(pos["qty"]))
        qty   = min(int(held[sym].get("qty", 0)), owned)
        if qty < 1:
            held.pop(sym, None)
            continue
        order = sell_qty(sym, qty)
        if order:
            px  = latest_price(sym) or float(pos["current_price"])
            pnl = (px - held[sym].get("entry_price", px)) * qty
            record_trade("superinvestor", sym, pnl, "rotate out")
            log.info(f"SELL {qty} {sym} @ ~${px:.2f} | P&L ${pnl:+.2f}")
            print(f"[SUPER] SELL {qty} {sym} | P&L ${pnl:+.2f}")
            held.pop(sym, None)

    # ── Buy new entrants up to the basket ──────────────────────────────────────
    weight   = get_weight("superinvestor")
    per_name = (ALLOCATION * weight) / TARGET_NAMES
    for sym in sorted(target):
        if sym in held and get_position(sym):
            continue   # already hold it — let it ride
        px = latest_price(sym)
        if not px:
            log.warning(f"No price for {sym} — skipping buy.")
            continue
        ok, reason = can_enter("superinvestor", sym, per_name)
        if not ok:
            log.warning(f"Risk guard blocked {sym}: {reason}")
            print(f"[SUPER] Risk guard blocked {sym} — {reason}")
            continue
        order = buy_notional(sym, per_name)
        if order:
            qty = per_name / px
            held[sym] = {"qty": qty, "entry_price": px}
            log.info(f"BUY {sym} ~${per_name:.0f} (~{qty:.2f} sh @ ${px:.2f})")
            print(f"[SUPER] BUY {sym} ~${per_name:.0f}")

    state["holdings"] = held
    state["last_rebalance_month"] = this_month
    state["last_filings"] = filings
    state.setdefault("history", []).append({
        "month": this_month,
        "target": sorted(target),
        "scores": {tk: scores[tk] for tk in sorted(target) if tk in scores},
    })
    save_state(state)
    log.info(f"Rebalanced for {this_month} — now holding {sorted(held)}")
    print(f"[SUPER] Rebalanced — holding {sorted(held)}")


if __name__ == "__main__":
    run()
