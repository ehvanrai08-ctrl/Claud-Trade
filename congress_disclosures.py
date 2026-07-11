"""
congress_disclosures.py — congressional trades straight from the official sources
=================================================================================
Free, keyless replacement for Quiver Quant's congress-trading API (which went
401/paywalled on 2026-07-08). Rather than depend on another third-party mirror
that can rot the same way (Senate/House Stock Watcher: dead S3 buckets;
Capitol Trades: dead CloudFront backend — all probed 2026-07-11), this reads
the statutory systems of record directly, the same pattern superinvestor_copy
proves out with SEC EDGAR:

- House: disclosures-clerk.house.gov — yearly ZIP index (TSV) of filings, then
  per-PTR PDFs. Only e-filed PTRs (DocID starting "2") are text-parseable with
  pypdf; paper-filed scans (~10%) are skipped.
- Senate: efdsearch.senate.gov — cookie/CSRF agreement handshake, JSON search
  for Periodic Transaction Reports, then each PTR's HTML table.

Records are normalized to the Quiver field shape copy_trader already speaks:
  {Representative, Ticker, Transaction, ReportDate, TransactionDate, Amount,
   Chamber}
with Transaction ∈ {"Purchase", "Sale (Full)", "Sale (Partial)"} and ISO dates.

Filings are cached in congress_cache.json (committed like other state) so the
hourly bot only downloads/parses NEW filings; per-run fetches are capped so a
cold cache warms over a few runs instead of blowing the runtime budget.
"""

import io
import json
import logging
import os
import re
import zipfile
import requests
from datetime import datetime, timedelta

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = f"{BASE_DIR}/congress_cache.json"

# Real contact in the UA — same courtesy SEC EDGAR demands (CLAUDE.md gotcha #7).
UA = {"User-Agent": "Claud-Trade paper-trading research (contact: ehvanrai08@gmail.com)"}

HOUSE_INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
HOUSE_PDF_URL   = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{docid}.pdf"
SENATE_BASE     = "https://efdsearch.senate.gov"

MAX_NEW_FILINGS_PER_RUN = 40   # per chamber; cold cache warms over a few runs
CACHE_MAX_AGE_DAYS      = 120

log = logging.getLogger(__name__)


def _iso(mdy):
    """'06/30/2026' → '2026-06-30' (None on garbage)."""
    try:
        return datetime.strptime(mdy.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return None


def _load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            log.warning("congress cache unreadable — rebuilding")
    return {"house": {}, "senate": {}}


def _save_cache(cache):
    cutoff = (datetime.now() - timedelta(days=CACHE_MAX_AGE_DAYS)).strftime("%Y-%m-%d")
    for chamber in ("house", "senate"):
        cache[chamber] = {k: v for k, v in cache[chamber].items()
                          if (v.get("report_date") or "9999") >= cutoff}
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=1)


# ── House ─────────────────────────────────────────────────────────────────────

def _house_filings(since_iso):
    """PTR filings from the yearly index ZIP(s): [(docid, 'First Last', iso_date)].
    Only e-filed ones (DocID starts '2') — paper scans can't be text-parsed."""
    years = {datetime.now().year}
    if since_iso < f"{datetime.now().year}-01-01":
        years.add(datetime.now().year - 1)
    out = []
    for year in sorted(years):
        r = requests.get(HOUSE_INDEX_URL.format(year=year), headers=UA, timeout=60)
        r.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(r.content))
        txt = next(n for n in z.namelist() if n.endswith(".txt"))
        for line in z.read(txt).decode("utf-8", "replace").strip().split("\n")[1:]:
            cols = line.split("\t")
            if len(cols) < 9 or cols[4].strip() != "P":
                continue
            docid = cols[8].strip()
            filed = _iso(cols[7])
            if not docid.startswith("2") or not filed or filed < since_iso:
                continue
            name = f"{cols[2].strip()} {cols[1].strip()}".strip()
            out.append((docid, name, filed))
    return out


# One transaction in flattened PTR text: "(CCI) [ST] S 06/30/202607/02/2026$1,001 - $15,000"
# (dates run together in extraction; type is P/S with optional "(partial)").
HOUSE_TX_RE = re.compile(
    r"\(([A-Z][A-Z.\-]{0,6})\)\s*\[ST\].{0,60}?"
    r"\b([PSE])\b\s*(\(partial\))?\s*"
    r"(\d{2}/\d{2}/\d{4})\s*(\d{2}/\d{2}/\d{4})\s*"
    r"(\$[\d,]+(?:\s*-\s*\$[\d,]+)?)", re.S)


def _parse_house_pdf(pdf_bytes):
    from pypdf import PdfReader
    text = " ".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf_bytes)).pages)
    trades = []
    for m in HOUSE_TX_RE.finditer(text):
        ticker, ttype, partial, txn_date, notif_date, amount = m.groups()
        if ttype == "E":     # exchanges aren't mirrorable
            continue
        trades.append({
            "Ticker":          ticker,
            "Transaction":     "Purchase" if ttype == "P"
                               else ("Sale (Partial)" if partial else "Sale (Full)"),
            "TransactionDate": _iso(txn_date),
            "Amount":          re.sub(r"\s+", " ", amount),
        })
    return trades


def _fetch_house(cache, since_iso):
    filings = _house_filings(since_iso)
    new = [(d, n, f) for d, n, f in filings if d not in cache["house"]]
    for docid, name, filed in sorted(new, key=lambda x: x[2], reverse=True)[:MAX_NEW_FILINGS_PER_RUN]:
        try:
            r = requests.get(HOUSE_PDF_URL.format(year=filed[:4], docid=docid),
                             headers=UA, timeout=60)
            r.raise_for_status()
            trades = _parse_house_pdf(r.content)
        except Exception as e:
            log.warning(f"House PTR {docid} ({name}) unparseable: {e}")
            trades = []
        cache["house"][docid] = {"name": name, "report_date": filed, "trades": trades}
    if len(new) > MAX_NEW_FILINGS_PER_RUN:
        log.info(f"House: {len(new) - MAX_NEW_FILINGS_PER_RUN} filings deferred to next run")


# ── Senate ────────────────────────────────────────────────────────────────────

def _senate_session():
    s = requests.Session()
    s.headers.update(UA)
    r = s.get(f"{SENATE_BASE}/search/home/", timeout=30)
    r.raise_for_status()
    m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', r.text)
    if not m:
        raise RuntimeError("eFD agreement page had no CSRF token")
    r = s.post(f"{SENATE_BASE}/search/home/",
               data={"prohibition_agreement": "1", "csrfmiddlewaretoken": m.group(1)},
               headers={"Referer": f"{SENATE_BASE}/search/home/"}, timeout=30)
    r.raise_for_status()
    return s


def _senate_filings(s, since_iso):
    """[(uuid, 'First Last', iso_filed_date)] via the JSON search endpoint."""
    start_mdy = datetime.strptime(since_iso, "%Y-%m-%d").strftime("%m/%d/%Y")
    out, start = [], 0
    while True:
        r = s.post(f"{SENATE_BASE}/search/report/data/",
                   data={"start": str(start), "length": "100",
                         "report_types": "[11]", "filer_types": "[]",
                         "submitted_start_date": f"{start_mdy} 00:00:00",
                         "submitted_end_date": "", "candidate_state": "",
                         "senator_state": "", "office_id": "",
                         "first_name": "", "last_name": ""},
                   headers={"Referer": f"{SENATE_BASE}/search/",
                            "X-CSRFToken": s.cookies.get("csrftoken")},
                   timeout=30)
        r.raise_for_status()
        rows = r.json().get("data", [])
        for first, last, _filer, link, filed in rows:
            m = re.search(r'href="/search/view/ptr/([0-9a-f\-]+)/"', link)
            if m:
                out.append((m.group(1), f"{first.strip()} {last.strip()}", _iso(filed)))
        if len(rows) < 100:
            return out
        start += 100


SENATE_TYPE = {"Purchase": "Purchase", "Sale (Full)": "Sale (Full)",
               "Sale (Partial)": "Sale (Partial)"}


def _parse_senate_ptr(html):
    """PTR page table → trades. Columns: #, Txn Date, Owner, Ticker, Asset,
    Asset Type, Type, Amount, Comment. Ticker is '--' for non-equities."""
    trades = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        tds = [re.sub(r"<[^>]+>", " ", td) for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        tds = [re.sub(r"\s+", " ", td).strip() for td in tds]
        if len(tds) < 8:
            continue
        ticker, ttype = tds[3], SENATE_TYPE.get(tds[6])
        if not ttype or not ticker or ticker == "--":
            continue
        trades.append({
            "Ticker":          ticker.split()[0].upper(),
            "Transaction":     ttype,
            "TransactionDate": _iso(tds[1]),
            "Amount":          tds[7],
        })
    return trades


def _fetch_senate(cache, since_iso):
    s = _senate_session()
    filings = _senate_filings(s, since_iso)
    new = [(u, n, f) for u, n, f in filings if u not in cache["senate"]]
    for uuid, name, filed in sorted(new, key=lambda x: x[2] or "", reverse=True)[:MAX_NEW_FILINGS_PER_RUN]:
        try:
            r = s.get(f"{SENATE_BASE}/search/view/ptr/{uuid}/", timeout=30)
            r.raise_for_status()
            trades = _parse_senate_ptr(r.text)
        except Exception as e:
            log.warning(f"Senate PTR {uuid} ({name}) unparseable: {e}")
            trades = []
        cache["senate"][uuid] = {"name": name, "report_date": filed, "trades": trades}


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_recent_trades(days=90):
    """All House+Senate PTR trades filed in the last `days`, Quiver-shaped.
    One chamber failing degrades to the other; BOTH failing raises (so the
    caller's consecutive-failure alerting still fires)."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    cache = _load_cache()
    errors = []
    for chamber, fetch in (("house", _fetch_house), ("senate", _fetch_senate)):
        try:
            fetch(cache, since)
        except Exception as e:
            errors.append(f"{chamber}: {e}")
            log.warning(f"{chamber} disclosure fetch failed: {e}")
    if len(errors) == 2:
        raise RuntimeError("both disclosure sources failed — " + "; ".join(errors))
    _save_cache(cache)

    out = []
    for chamber in ("house", "senate"):
        for filing in cache[chamber].values():
            if (filing.get("report_date") or "") < since:
                continue
            for t in filing["trades"]:
                out.append({
                    "Representative":  filing["name"],
                    "ReportDate":      filing["report_date"],
                    "Chamber":         chamber,
                    **t,
                })
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    trades = fetch_recent_trades(days=45)
    print(f"{len(trades)} trades from the last 45 days")
    for t in sorted(trades, key=lambda x: x["ReportDate"], reverse=True)[:15]:
        print(f"  {t['ReportDate']} [{t['Chamber']:6}] {t['Representative']:28} "
              f"{t['Transaction']:14} {t['Ticker']:6} {t['Amount']}")
