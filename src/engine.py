"""Event-driven backtest engine.

Design rules, in order of importance:

1. NO LOOK-AHEAD. A strategy sees bars[0..i] when deciding at bar i. It fills
   at bar i+1's open. There is no path by which future data reaches a decision.
2. PESSIMISTIC INTRABAR RESOLUTION. If a bar's range touches both the stop and
   the target, the engine assumes the STOP filled first. Real intrabar
   sequencing is unknowable from OHLC; assuming the good outcome is the single
   most common way backtests lie.
3. COSTS ALWAYS APPLY. There is no cost-free mode.
4. RISK LAYER IS AUTHORITATIVE. Strategies never size themselves.
"""
from dataclasses import dataclass, asdict
from datetime import time
import pandas as pd

from .costs import CostModel, BASE
from .risk import RiskManager, RiskConfig


@dataclass
class Signal:
    side: str      # "long" | "short"
    stop: float
    target: float
    tag: str = ""


@dataclass
class Trade:
    symbol: str
    side: str
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: pd.Timestamp
    exit_price: float
    qty: int
    stop: float
    target: float
    gross_pnl: float
    costs: float
    net_pnl: float
    r_multiple: float
    exit_reason: str
    tag: str


class Backtest:
    def __init__(self, strategy, cost_model: CostModel = BASE,
                 risk_cfg: RiskConfig | None = None,
                 starting_equity: float = 100_000.0,
                 session_close: time = time(15, 55)):
        self.strategy = strategy
        self.costs = cost_model
        self.risk = RiskManager(risk_cfg or RiskConfig(), starting_equity)
        self.starting_equity = starting_equity
        self.session_close = session_close
        self.trades: list[Trade] = []

    def run(self, bars: dict[str, pd.DataFrame]) -> pd.DataFrame:
        """bars: {symbol: DataFrame[open,high,low,close,volume], tz-aware ET index}"""
        for symbol, df in bars.items():
            if df.empty:
                continue
            df = df.sort_index()
            for day, day_bars in df.groupby(df.index.date):
                self.risk.new_day(day)
                day_bars = self.strategy.prepare(day_bars)
                self.strategy.new_day(symbol, day_bars)
                self._run_day(symbol, day_bars)
        return self.to_frame()

    def _run_day(self, symbol: str, day_bars: pd.DataFrame) -> None:
        position = None
        n = len(day_bars)

        for i in range(n):
            ts = day_bars.index[i]
            bar = day_bars.iloc[i]

            # --- manage existing position on THIS bar -------------------
            if position is not None:
                exit_price, reason = self._check_exit(position, bar, ts)
                if exit_price is not None:
                    self._close(position, exit_price, ts, reason)
                    position = None
                    continue

            # --- forced flat at session close ---------------------------
            if position is not None and ts.time() >= self.session_close:
                self._close(position, bar["close"], ts, "session_close")
                position = None
                continue

            if position is not None or i + 1 >= n:
                continue
            if ts.time() >= self.session_close:
                continue

            # --- strategy decides using bars[0..i] ONLY -----------------
            # We pass the full frame plus the current index rather than a slice:
            # slicing every bar is O(n^2). Strategies are contractually forbidden
            # from reading beyond position i, and prepare() precomputes only
            # causal (rolling/expanding) columns.
            sig = self.strategy.on_bar(symbol, day_bars, i)
            if sig is None or not self.risk.can_enter():
                continue

            # --- fill at NEXT bar's open --------------------------------
            fill_ts = day_bars.index[i + 1]
            raw_fill = day_bars.iloc[i + 1]["open"]
            qty = self.risk.size_position(raw_fill, sig.stop)
            if qty <= 0:
                continue

            # Sanity: stop must be on the correct side of the fill.
            if sig.side == "long" and sig.stop >= raw_fill:
                continue
            if sig.side == "short" and sig.stop <= raw_fill:
                continue

            entry_cost = self.costs.entry_cost(raw_fill, qty)
            self.risk.on_trade_opened()
            position = dict(symbol=symbol, side=sig.side, entry_time=fill_ts,
                            entry_price=raw_fill, qty=qty, stop=sig.stop,
                            target=sig.target, entry_cost=entry_cost, tag=sig.tag)

        if position is not None:
            self._close(position, day_bars.iloc[-1]["close"],
                        day_bars.index[-1], "eod_flat")

    def _check_exit(self, pos, bar, ts):
        """Pessimistic: stop is checked before target on every bar."""
        if pos["entry_time"] >= ts:
            return None, ""
        hi, lo = bar["high"], bar["low"]
        if pos["side"] == "long":
            if lo <= pos["stop"]:
                return pos["stop"], "stop"
            if hi >= pos["target"]:
                return pos["target"], "target"
        else:
            if hi >= pos["stop"]:
                return pos["stop"], "stop"
            if lo <= pos["target"]:
                return pos["target"], "target"
        return None, ""

    def _close(self, pos, exit_price, exit_time, reason):
        qty = pos["qty"]
        if pos["side"] == "long":
            gross = (exit_price - pos["entry_price"]) * qty
            exit_cost = self.costs.exit_cost(exit_price, qty, is_sell=True)
        else:
            gross = (pos["entry_price"] - exit_price) * qty
            exit_cost = self.costs.exit_cost(exit_price, qty, is_sell=False)

        total_costs = pos["entry_cost"] + exit_cost
        net = gross - total_costs
        risk_dollars = abs(pos["entry_price"] - pos["stop"]) * qty
        r = net / risk_dollars if risk_dollars > 0 else 0.0

        self.trades.append(Trade(
            symbol=pos["symbol"], side=pos["side"], entry_time=pos["entry_time"],
            entry_price=pos["entry_price"], exit_time=exit_time,
            exit_price=exit_price, qty=qty, stop=pos["stop"], target=pos["target"],
            gross_pnl=gross, costs=total_costs, net_pnl=net, r_multiple=r,
            exit_reason=reason, tag=pos["tag"]))
        self.risk.on_trade_closed(net)

    def to_frame(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame()
        return pd.DataFrame([asdict(t) for t in self.trades])


def stats(trades: pd.DataFrame, starting_equity: float = 100_000.0) -> dict:
    if trades.empty:
        return {"trades": 0}
    r = trades["r_multiple"]
    wins, losses = r[r > 0], r[r <= 0]
    eq = starting_equity + trades["net_pnl"].cumsum()
    peak = eq.cummax()
    dd = (eq - peak) / peak
    n_days = trades["entry_time"].dt.date.nunique()

    streak = cur = 0
    for x in r:
        cur = cur + 1 if x <= 0 else 0
        streak = max(streak, cur)

    return {
        "trades": len(trades),
        "trading_days": n_days,
        "trades_per_day": round(len(trades) / n_days, 2) if n_days else 0,
        "win_rate": round(len(wins) / len(r), 4),
        "avg_win_R": round(wins.mean(), 3) if len(wins) else 0.0,
        "avg_loss_R": round(losses.mean(), 3) if len(losses) else 0.0,
        "expectancy_R": round(r.mean(), 4),
        "total_R": round(r.sum(), 2),
        "gross_pnl": round(trades["gross_pnl"].sum(), 2),
        "total_costs": round(trades["costs"].sum(), 2),
        "net_pnl": round(trades["net_pnl"].sum(), 2),
        "pnl_per_day": round(trades["net_pnl"].sum() / n_days, 2) if n_days else 0,
        "return_pct": round(trades["net_pnl"].sum() / starting_equity * 100, 3),
        "max_drawdown_pct": round(dd.min() * 100, 2),
        "max_consec_losses": streak,
        "profit_factor": round(trades.loc[trades.net_pnl > 0, "net_pnl"].sum() /
                               abs(trades.loc[trades.net_pnl < 0, "net_pnl"].sum()), 3)
        if (trades["net_pnl"] < 0).any() else float("inf"),
    }
