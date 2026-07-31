"""Alpaca PAPER execution loop.

Hard safety properties, in order:
  1. Refuses to start unless the resolved endpoint is the PAPER endpoint.
  2. The risk layer is checked before every order. No strategy can bypass it.
  3. Every entry is a BRACKET order -- the stop is submitted atomically with
     the entry, so a crash between entry and stop placement cannot leave a
     naked position.
  4. Flattens everything before the close. No overnight risk, ever.

Verified against alpaca-py 0.43.5.
"""
import os, time as _time
from datetime import datetime, timedelta, time as dtime
import pandas as pd

from .risk import RiskManager, RiskConfig
from .journal import Journal

NY = "America/New_York"


class PaperExecutor:
    def __init__(self, strategy, symbols, risk_cfg=None, feed="iex",
                 flatten_at=dtime(15, 50), poll_seconds=60, journal_path="results/live_journal.csv"):
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient

        key, sec = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
        if not key or not sec:
            raise RuntimeError("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY.")

        # --- SAFETY GATE 1 -------------------------------------------------
        if os.getenv("ALPACA_PAPER", "true").lower() != "true":
            raise RuntimeError(
                "ALPACA_PAPER is not 'true'. This module is paper-only by design. "
                "Going live is a deliberate act that should not happen via an env var typo.")

        self.trading = TradingClient(key, sec, paper=True)
        if "paper" not in str(self.trading._base_url).lower():
            raise RuntimeError(f"Refusing to run: endpoint is not paper ({self.trading._base_url})")

        self.data = StockHistoricalDataClient(key, sec)
        self.strategy = strategy
        self.symbols = symbols if isinstance(symbols, list) else [symbols]
        self.feed = feed
        self.flatten_at = flatten_at
        self.poll_seconds = poll_seconds
        acct = self.trading.get_account()
        self.risk = RiskManager(risk_cfg or RiskConfig(), float(acct.equity))
        self.journal = Journal(journal_path)
        self.prepared = {}
        print(f"[init] PAPER account equity ${float(acct.equity):,.2f}")

    # ------------------------------------------------------------------
    def _recent_bars(self, symbol, lookback_min=400):
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        end = datetime.utcnow()
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(1, TimeFrameUnit.Minute),
            start=end - timedelta(minutes=lookback_min),
            end=end, feed=self.feed, adjustment="all")
        df = self.data.get_stock_bars(req).df
        if df.empty:
            return None
        df = df.reset_index()
        if "symbol" in df.columns:
            df = df[df["symbol"] == symbol]
        df = df.set_index("timestamp").tz_convert(NY)
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        today = datetime.now(pd.Timestamp.now(tz=NY).tzinfo).date()
        return df[df.index.date == today].between_time("09:30", "16:00")

    def _submit_bracket(self, symbol, sig, qty, ref_price):
        from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
        side = OrderSide.BUY if sig.side == "long" else OrderSide.SELL
        order = MarketOrderRequest(
            symbol=symbol, qty=qty, side=side,
            time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=round(float(sig.target), 2)),
            stop_loss=StopLossRequest(stop_price=round(float(sig.stop), 2)))
        o = self.trading.submit_order(order_data=order)
        self.journal.log_entry(symbol, sig, qty, ref_price, o.id)
        print(f"[entry] {sig.side.upper():5s} {qty:>5d} {symbol} @~{ref_price:.2f} "
              f"stop {sig.stop:.2f} tgt {sig.target:.2f} tag={sig.tag}")
        return o

    def flatten_all(self, reason=""):
        try:
            self.trading.cancel_orders()
            self.trading.close_all_positions(cancel_orders=True)
            print(f"[flat] all positions closed. {reason}")
        except Exception as e:
            print(f"[flat] error: {e}")

    # ------------------------------------------------------------------
    def run_session(self):
        clock = self.trading.get_clock()
        if not clock.is_open:
            print(f"[skip] market closed. next open {clock.next_open}")
            return
        self.risk.new_day(pd.Timestamp.now(tz=NY).date())

        while True:
            now = pd.Timestamp.now(tz=NY)
            if now.time() >= self.flatten_at:
                self.flatten_all("session end")
                break
            if not self.trading.get_clock().is_open:
                self.flatten_all("market closed"); break

            acct = self.trading.get_account()
            self.risk.state.equity = float(acct.equity)
            self.risk.state.open_positions = len(self.trading.get_all_positions())

            if self.risk.halted:
                print(f"[HALT] {self.risk.state.halted_reason} -- flattening, done for the day")
                self.flatten_all("risk halt"); break

            for sym in self.symbols:
                try:
                    bars = self._recent_bars(sym)
                    if bars is None or len(bars) < 25:
                        continue
                    bars = self.strategy.prepare(bars)
                    if sym not in self.prepared:
                        self.strategy.new_day(sym, bars); self.prepared[sym] = True
                    else:
                        self.strategy.times = bars.index
                    if any(p.symbol == sym for p in self.trading.get_all_positions()):
                        continue
                    if not self.risk.can_enter():
                        continue
                    sig = self.strategy.on_bar(sym, bars, len(bars) - 1)
                    if sig is None:
                        continue
                    px = float(bars["close"].iat[-1])
                    qty = self.risk.size_position(px, sig.stop)
                    if qty <= 0:
                        continue
                    self._submit_bracket(sym, sig, qty, px)
                    self.risk.on_trade_opened()
                except Exception as e:
                    print(f"[err] {sym}: {e}")
            _time.sleep(self.poll_seconds)
