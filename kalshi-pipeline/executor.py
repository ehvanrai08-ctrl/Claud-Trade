from __future__ import annotations

import asyncio
import uuid

import httpx

import config
import logger
import kalshi_auth
from edge import Signal


def execute_trade(signal: Signal) -> dict:
    """Execute a trade on Kalshi or log a dry-run. Synchronous."""
    daily_spent = abs(logger.get_daily_pnl())
    if daily_spent + signal.bet_amount > config.DAILY_LOSS_LIMIT_USD:
        return _log_and_return(signal, status="rejected_daily_limit", order_id=None)

    if config.DRY_RUN:
        return _log_and_return(signal, status="dry_run", order_id=None)

    return _execute_live(signal)


async def execute_trade_async(signal: Signal) -> dict:
    """Async wrapper around execute_trade."""
    return await asyncio.get_event_loop().run_in_executor(None, execute_trade, signal)


def _execute_live(signal: Signal) -> dict:
    """Place a real order via the Kalshi trade API."""
    if not kalshi_auth.is_configured():
        return _log_and_return(signal, status="error_no_credentials", order_id=None)

    try:
        ticker = signal.market.condition_id
        side = signal.side.lower()  # "yes" / "no"

        # Kalshi prices are integer cents (1..99). Pick the side's price.
        prob = signal.market.yes_price if side == "yes" else signal.market.no_price
        price_cents = max(1, min(99, int(round(prob * 100))))

        # Kalshi sizes orders in whole contracts, not dollars.
        # Each contract costs price_cents/100 USD; convert the bet to a count.
        cost_per_contract = price_cents / 100.0
        count = max(1, int(signal.bet_amount / cost_per_contract))

        body = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "action": "buy",
            "side": side,
            "count": count,
            "type": "limit",
            f"{side}_price": price_cents,
        }

        path = f"{config.KALSHI_API_PREFIX}/portfolio/orders"
        resp = httpx.post(
            f"{config.KALSHI_HOST}/portfolio/orders",
            json=body,
            headers=kalshi_auth.sign("POST", path),
            timeout=15,
        )
        resp.raise_for_status()
        order = resp.json().get("order", {})
        order_id = order.get("order_id", "unknown")
        return _log_and_return(signal, status="executed", order_id=order_id)

    except httpx.HTTPStatusError as e:
        return _log_and_return(signal, status=f"error_http_{e.response.status_code}", order_id=None)
    except Exception as e:
        return _log_and_return(signal, status=f"error_{type(e).__name__}", order_id=None)


def _log_and_return(signal: Signal, status: str, order_id: str | None) -> dict:
    """Log trade to SQLite and return result dict."""
    trade_id = logger.log_trade(
        market_id=signal.market.condition_id,
        market_question=signal.market.question,
        claude_score=signal.claude_score,
        market_price=signal.market_price,
        edge=signal.edge,
        side=signal.side,
        amount_usd=signal.bet_amount,
        order_id=order_id,
        status=status,
        reasoning=signal.reasoning,
        headlines=signal.headlines,
        news_source=signal.news_source,
        classification=signal.classification,
        materiality=signal.materiality,
        news_latency_ms=signal.news_latency_ms,
        classification_latency_ms=signal.classification_latency_ms,
        total_latency_ms=signal.total_latency_ms,
    )

    return {
        "trade_id": trade_id,
        "market": signal.market.question,
        "side": signal.side,
        "amount": signal.bet_amount,
        "edge": signal.edge,
        "status": status,
        "order_id": order_id,
        "classification": signal.classification,
        "materiality": signal.materiality,
        "latency_ms": signal.total_latency_ms,
    }
