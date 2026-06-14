from __future__ import annotations

from dataclasses import dataclass

import httpx

import config
import kalshi_auth


@dataclass
class Market:
    condition_id: str          # Kalshi ticker (e.g. "KXAIWINNER-25") — kept named
    question: str              # for drop-in compatibility with the rest of the pipeline
    category: str
    yes_price: float           # 0..1 probability (Kalshi quotes cents; divided here)
    no_price: float
    volume: float              # CONTRACTS on Kalshi, not USD
    end_date: str
    active: bool
    tokens: list[dict]         # unused on Kalshi; kept for dataclass compatibility

    @property
    def implied_probability(self) -> float:
        return self.yes_price


def _cents(value) -> float:
    """Kalshi prices are integer cents (1..99). Convert to a 0..1 probability."""
    try:
        return float(value) / 100.0
    except (TypeError, ValueError):
        return 0.5


def fetch_active_markets(limit: int = 50) -> list[Market]:
    """Fetch open markets from Kalshi's trade API."""
    markets: list[Market] = []
    path = f"{config.KALSHI_API_PREFIX}/markets"

    try:
        resp = httpx.get(
            f"{config.KALSHI_HOST}/markets",
            params={"limit": min(limit, 1000), "status": "open"},
            headers=kalshi_auth.sign("GET", path),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[markets] Kalshi API error: {e}")
        return markets

    for m in data.get("markets", []):
        try:
            # Prefer the ask (what you'd pay to take the side); fall back to last price.
            yes_ask = m.get("yes_ask")
            no_ask = m.get("no_ask")
            yes_price = _cents(yes_ask) if yes_ask else _cents(m.get("last_price", 50))
            no_price = _cents(no_ask) if no_ask else (1.0 - yes_price)

            question = m.get("title") or m.get("subtitle") or m.get("ticker", "")
            vol = float(m.get("volume", 0) or 0)

            # Skip already-decided or empty markets.
            if yes_price in (0.0, 1.0) and vol == 0:
                continue

            markets.append(Market(
                condition_id=m.get("ticker", ""),
                question=question,
                category=_infer_category(question, [m.get("category", "")]),
                yes_price=yes_price,
                no_price=no_price,
                volume=vol,
                end_date=m.get("close_time", ""),
                active=m.get("status", "open") == "open",
                tokens=[],
            ))
        except (KeyError, ValueError, TypeError):
            continue

    markets.sort(key=lambda x: x.volume, reverse=True)
    return markets


def _infer_category(question: str, tags: list) -> str:
    """Infer category from question text and tags."""
    q = question.lower()
    tag_str = " ".join(str(t).lower() for t in tags)
    combined = f"{q} {tag_str}"

    if any(kw in combined for kw in ["ai", "artificial intelligence", "openai", "chatgpt", "llm", "google ai", "anthropic"]):
        return "ai"
    if any(kw in combined for kw in ["bitcoin", "ethereum", "crypto", "blockchain", "defi", "token"]):
        return "crypto"
    if any(kw in combined for kw in ["election", "president", "congress", "senate", "trump", "biden", "political"]):
        return "politics"
    if any(kw in combined for kw in ["spacex", "nasa", "climate", "research", "study", "discovery"]):
        return "science"
    if any(kw in combined for kw in ["tech", "apple", "google", "microsoft", "software", "startup"]):
        return "technology"
    return "other"


def filter_by_categories(markets: list[Market], categories: list[str] | None = None) -> list[Market]:
    """Filter markets to only target categories."""
    cats = categories or config.MARKET_CATEGORIES
    return [m for m in markets if m.category in cats]


def get_token_id(market: Market, side: str) -> str | None:
    """
    Compatibility shim. Kalshi has no per-side token IDs — direction is expressed
    with side="yes"/"no" on the order, against the market ticker.
    """
    return market.condition_id


if __name__ == "__main__":
    all_markets = fetch_active_markets(limit=50)
    filtered = filter_by_categories(all_markets)
    print(f"\n--- {len(filtered)} markets in target categories (of {len(all_markets)} total) ---\n")
    for m in filtered[:15]:
        print(f"  [{m.category}] {m.question}")
        print(f"    YES: {m.yes_price:.2f} | NO: {m.no_price:.2f} | Vol: {m.volume:,.0f} contracts")
        print()
