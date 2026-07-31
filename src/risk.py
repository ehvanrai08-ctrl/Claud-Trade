"""Risk layer.

Sits ABOVE strategy logic. A strategy proposes; the risk layer disposes.
No strategy can size its own position, and no strategy can override a halt.
This separation is the whole point -- it is what makes a bad strategy lose
slowly instead of catastrophically.
"""
from dataclasses import dataclass, field
from datetime import date
import math


@dataclass
class RiskConfig:
    risk_per_trade: float = 0.005        # 0.5% of equity risked per trade
    max_daily_loss: float = 0.02         # halt trading for the day at -2%
    max_consecutive_losses: int = 4      # halt for the day after N losers
    max_open_positions: int = 3
    # Sanity backstop only -- NOT the primary risk control. Stop distance is
    # what defines risk intraday. If this binds before size_position's
    # risk-based calc, your realised per-trade risk silently drifts below
    # target and your expectancy math stops describing reality.
    max_position_pct: float = 1.00       # notional cap per position
    max_trades_per_day: int = 20         # overtrading circuit breaker


@dataclass
class RiskState:
    equity: float
    start_of_day_equity: float
    day: date | None = None
    consecutive_losses: int = 0
    trades_today: int = 0
    open_positions: int = 0
    halted_reason: str | None = None
    halt_log: list = field(default_factory=list)


class RiskManager:
    def __init__(self, cfg: RiskConfig, starting_equity: float):
        self.cfg = cfg
        self.state = RiskState(equity=starting_equity,
                               start_of_day_equity=starting_equity)

    def new_day(self, d: date) -> None:
        self.state.day = d
        self.state.start_of_day_equity = self.state.equity
        self.state.consecutive_losses = 0
        self.state.trades_today = 0
        self.state.halted_reason = None

    @property
    def halted(self) -> bool:
        return self.state.halted_reason is not None

    def _halt(self, reason: str) -> None:
        if not self.state.halted_reason:
            self.state.halted_reason = reason
            self.state.halt_log.append((self.state.day, reason))

    def check_halts(self) -> None:
        s, c = self.state, self.cfg
        dd = (s.equity - s.start_of_day_equity) / s.start_of_day_equity
        if dd <= -c.max_daily_loss:
            self._halt(f"daily loss limit hit ({dd:.2%})")
        if s.consecutive_losses >= c.max_consecutive_losses:
            self._halt(f"{s.consecutive_losses} consecutive losses")
        if s.trades_today >= c.max_trades_per_day:
            self._halt(f"max trades/day ({s.trades_today})")

    def can_enter(self) -> bool:
        self.check_halts()
        return (not self.halted) and self.state.open_positions < self.cfg.max_open_positions

    def size_position(self, entry: float, stop: float) -> int:
        """Risk-based sizing. Returns share count, or 0 if the trade is not takeable.

        This is the ONLY place position size is computed.
        """
        stop_distance = abs(entry - stop)
        if stop_distance <= 0 or entry <= 0:
            return 0
        risk_dollars = self.state.equity * self.cfg.risk_per_trade
        qty = math.floor(risk_dollars / stop_distance)
        # Notional cap -- prevents a tight stop from producing an absurd position.
        max_notional = self.state.equity * self.cfg.max_position_pct
        qty = min(qty, math.floor(max_notional / entry))
        return max(qty, 0)

    def on_trade_closed(self, pnl: float) -> None:
        s = self.state
        s.equity += pnl
        s.trades_today += 1
        s.open_positions = max(0, s.open_positions - 1)
        s.consecutive_losses = s.consecutive_losses + 1 if pnl < 0 else 0
        self.check_halts()

    def on_trade_opened(self) -> None:
        self.state.open_positions += 1
