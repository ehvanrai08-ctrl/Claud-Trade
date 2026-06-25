"""
broker.py — thin Alpaca wrapper shared by all strategy bots.

All HTTP calls go through this module. To switch brokers, only this file changes.
Loaded via: from broker import Broker; api = Broker()
"""

import os
import time
import requests
from dotenv import dotenv_values

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_config = dotenv_values(f"{BASE_DIR}/.env")

TRADE_URL = _config.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
DATA_URL  = "https://data.alpaca.markets/v2"

_TRADE_HEADERS = {
    "APCA-API-KEY-ID":     _config.get("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": _config.get("ALPACA_SECRET_KEY", ""),
    "Content-Type": "application/json",
}
_DATA_HEADERS = {
    "APCA-API-KEY-ID":     _config.get("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": _config.get("ALPACA_SECRET_KEY", ""),
}


class BrokerError(Exception):
    pass


class Broker:
    # ------------------------------------------------------------------ #
    # internal helpers
    # ------------------------------------------------------------------ #

    def _trade(self, method, path, **kwargs):
        kwargs.setdefault("headers", _TRADE_HEADERS)
        kwargs.setdefault("timeout", 15)
        r = requests.request(method, f"{TRADE_URL}{path}", **kwargs)
        return r

    def _data(self, path, params=None):
        r = requests.get(f"{DATA_URL}{path}", headers=_DATA_HEADERS,
                         params=params, timeout=30)
        return r

    # ------------------------------------------------------------------ #
    # market status
    # ------------------------------------------------------------------ #

    def is_market_open(self):
        r = self._trade("GET", "/clock")
        return r.json().get("is_open", False) if r.ok else False

    def clock(self):
        """Return full clock dict: is_open, next_open, next_close."""
        r = self._trade("GET", "/clock")
        return r.json() if r.ok else {}

    # ------------------------------------------------------------------ #
    # account
    # ------------------------------------------------------------------ #

    def get_account(self):
        r = self._trade("GET", "/account")
        return r.json() if r.ok else {}

    def get_equity(self):
        a = self.get_account()
        return float(a.get("equity", 0))

    def get_cash(self):
        a = self.get_account()
        return float(a.get("cash", 0))

    def get_buying_power(self):
        a = self.get_account()
        return float(a.get("buying_power", 0))

    # ------------------------------------------------------------------ #
    # positions
    # ------------------------------------------------------------------ #

    def get_position(self, symbol):
        """
        Return position dict, None if flat, or "ERROR" sentinel on API failure.
        Callers must check for the "ERROR" sentinel before acting.
        """
        r = self._trade("GET", f"/positions/{symbol}")
        if r.status_code == 404:
            return None
        if r.ok:
            return r.json()
        return "ERROR"

    def get_all_positions(self):
        """Return list of all open position dicts, or [] on failure."""
        r = self._trade("GET", "/positions")
        return r.json() if r.ok else []

    def close_position(self, symbol):
        """Market-close entire position. Returns order dict or None."""
        r = self._trade("DELETE", f"/positions/{symbol}")
        return r.json() if r.ok else None

    # ------------------------------------------------------------------ #
    # orders — place
    # ------------------------------------------------------------------ #

    def market_order(self, symbol, side, qty=None, notional=None, tif="day"):
        body = {"symbol": symbol, "side": side, "type": "market", "time_in_force": tif}
        if notional is not None:
            body["notional"] = str(notional)
        else:
            body["qty"] = str(qty)
        r = self._trade("POST", "/orders", json=body)
        return r.json() if r.ok else None

    def stop_limit_order(self, symbol, side, qty, stop_price, limit_price, tif="day"):
        body = {
            "symbol": symbol, "side": side, "qty": str(qty),
            "type": "stop_limit", "time_in_force": tif,
            "stop_price": str(round(stop_price, 2)),
            "limit_price": str(round(limit_price, 2)),
        }
        r = self._trade("POST", "/orders", json=body)
        return r.json() if r.ok else None

    def bracket_order(self, symbol, side, qty, stop_loss_price, take_profit_price=None, tif="day"):
        """
        Bracket order with embedded stop-loss leg. stop_loss_price is a stop_limit ~1% worse.
        """
        sl_limit = round(stop_loss_price * (0.99 if side == "buy" else 1.01), 2)
        stop_loss = {"stop_price": str(round(stop_loss_price, 2)),
                     "limit_price": str(sl_limit)}
        body = {
            "symbol": symbol, "side": side, "qty": str(qty),
            "type": "market", "time_in_force": tif,
            "order_class": "bracket",
            "stop_loss": stop_loss,
        }
        if take_profit_price is not None:
            body["take_profit"] = {"limit_price": str(round(take_profit_price, 2))}
        r = self._trade("POST", "/orders", json=body)
        return r.json() if r.ok else None

    def sell_stop_limit(self, symbol, qty, stop_price, tif="gtc"):
        """Trailing stop replacement — sell stop_limit sitting below market."""
        limit_price = round(stop_price * 0.99, 2)
        return self.stop_limit_order(symbol, "sell", qty, stop_price, limit_price, tif=tif)

    # ------------------------------------------------------------------ #
    # orders — query / cancel
    # ------------------------------------------------------------------ #

    def get_order(self, order_id):
        r = self._trade("GET", f"/orders/{order_id}")
        return r.json() if r.ok else None

    def cancel_order(self, order_id):
        r = self._trade("DELETE", f"/orders/{order_id}")
        return r.ok

    def list_open_orders(self, symbol=None):
        params = {"status": "open", "limit": 100}
        if symbol:
            params["symbols"] = symbol
        r = self._trade("GET", "/orders", params=params)
        return r.json() if r.ok else []

    def fill_price(self, order_id, retries=6, delay=1.0):
        """
        Poll until order is filled; return filled_avg_price or None.
        Needed because market orders are not immediately filled on first GET.
        """
        for _ in range(retries):
            o = self.get_order(order_id)
            if o and o.get("status") == "filled":
                fp = o.get("filled_avg_price")
                return float(fp) if fp else None
            time.sleep(delay)
        return None

    # ------------------------------------------------------------------ #
    # assets
    # ------------------------------------------------------------------ #

    def is_tradeable(self, symbol):
        r = self._trade("GET", f"/assets/{symbol}")
        if not r.ok:
            return False
        a = r.json()
        return a.get("tradable") and a.get("status") == "active" and \
               a.get("asset_class") == "us_equity"

    # ------------------------------------------------------------------ #
    # market data — prices
    # ------------------------------------------------------------------ #

    def latest_price(self, symbol):
        """Return latest trade price as float, or None on failure."""
        r = self._data(f"/stocks/{symbol}/trades/latest")
        if r.ok:
            t = r.json().get("trade", {})
            p = t.get("p")
            return float(p) if p else None
        return None

    # ------------------------------------------------------------------ #
    # market data — bars
    # ------------------------------------------------------------------ #

    def get_bars(self, symbol, timeframe, start, limit=10000, sort="asc", adjustment="raw"):
        """
        Return list of bar dicts for a single symbol.
        timeframe examples: "1Min", "5Min", "1Day"
        """
        params = {
            "timeframe": timeframe, "start": f"{start}T00:00:00Z",
            "limit": limit, "sort": sort, "adjustment": adjustment,
        }
        bars, token = [], None
        while True:
            if token:
                params["page_token"] = token
            r = self._data(f"/stocks/{symbol}/bars", params=params)
            if not r.ok:
                break
            j = r.json()
            bars.extend(j.get("bars") or [])
            token = j.get("next_page_token")
            if not token:
                break
        return bars

    def get_bars_multi(self, symbols, timeframe, start, limit=10000, sort="asc", adjustment="raw"):
        """
        Batch bar fetch for multiple symbols. Returns {symbol: [bars]} dict.
        """
        params = {
            "symbols": ",".join(symbols), "timeframe": timeframe,
            "start": f"{start}T00:00:00Z", "limit": limit,
            "sort": sort, "adjustment": adjustment,
        }
        result = {s: [] for s in symbols}
        token = None
        while True:
            if token:
                params["page_token"] = token
            r = self._data("/stocks/bars", params=params)
            if not r.ok:
                break
            j = r.json()
            for sym, bars in (j.get("bars") or {}).items():
                result.setdefault(sym, []).extend(bars)
            token = j.get("next_page_token")
            if not token:
                break
        return result

    # ------------------------------------------------------------------ #
    # options (wheel strategy)
    # ------------------------------------------------------------------ #

    def find_option_contracts(self, underlying, opt_type, exp_gte, exp_lte,
                               strike_gte=None, strike_lte=None, limit=100):
        params = {
            "underlying_symbols": underlying, "type": opt_type,
            "expiration_date_gte": exp_gte, "expiration_date_lte": exp_lte,
            "limit": limit,
        }
        if strike_gte is not None:
            params["strike_price_gte"] = str(strike_gte)
        if strike_lte is not None:
            params["strike_price_lte"] = str(strike_lte)
        r = self._trade("GET", "/options/contracts", params=params)
        return r.json().get("option_contracts", []) if r.ok else []

    def latest_option_price(self, contract_symbol):
        r = self._data("/options/trades/latest", params={"symbols": contract_symbol})
        if r.ok:
            trades = r.json().get("trades", {})
            t = trades.get(contract_symbol, {})
            p = t.get("p")
            return float(p) if p else None
        return None

    def place_option_order(self, symbol, side, qty, order_type="market", tif="day",
                            limit_price=None):
        body = {"symbol": symbol, "side": side, "qty": str(qty),
                "type": order_type, "time_in_force": tif}
        if limit_price is not None:
            body["limit_price"] = str(round(limit_price, 2))
        r = self._trade("POST", "/orders", json=body)
        return r.json() if r.ok else None
