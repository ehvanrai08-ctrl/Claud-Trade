"""Transaction cost model.

The single most important file in this repo. A strategy that is profitable
before costs and unprofitable after costs is not a strategy. Every backtest
runs through here, and validate.py sweeps these numbers to show how sensitive
the edge is to them.

Defaults are deliberately pessimistic. If the edge only survives optimistic
costs, it does not survive.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    # Alpaca charges $0 commission on US equities. Do not set this to 0 and
    # then forget the rest of this file exists -- commission is the smallest
    # component of real cost for an intraday strategy.
    commission_per_share: float = 0.0
    commission_min: float = 0.0

    # Half the quoted bid/ask spread, in basis points of price. You pay this
    # crossing in AND crossing out. For SPY-tier liquidity this is ~0.1-0.3bp;
    # for a $50 large cap with a 1c spread it is ~1bp. Default 1.0bp is
    # conservative for large caps and wildly optimistic for small caps.
    half_spread_bps: float = 1.0

    # Adverse movement between signal and fill, plus queue/impact effects that
    # paper trading does NOT simulate. Alpaca paper fills are frictionless;
    # live fills are not. This is the term that closes that gap.
    slippage_bps: float = 2.0

    # Regulatory fees charged on SELLS only (SEC Section 31 + FINRA TAF).
    # These rates are revised periodically -- VERIFY CURRENT VALUES before
    # trusting live P&L reconciliation. Defaults below are placeholders that
    # are intentionally on the high side.
    sec_fee_rate: float = 0.0000278      # of sell notional  [VERIFY]
    finra_taf_per_share: float = 0.000166  # per share sold   [VERIFY]
    finra_taf_cap: float = 8.30            # per trade        [VERIFY]

    def entry_cost(self, price: float, qty: int) -> float:
        """Total dollar cost of establishing a position."""
        notional = price * qty
        spread = notional * (self.half_spread_bps / 10_000)
        slip = notional * (self.slippage_bps / 10_000)
        comm = max(self.commission_per_share * qty, self.commission_min) if qty else 0.0
        return spread + slip + comm

    def exit_cost(self, price: float, qty: int, is_sell: bool = True) -> float:
        """Total dollar cost of closing a position. Regulatory fees apply to sells."""
        notional = price * qty
        spread = notional * (self.half_spread_bps / 10_000)
        slip = notional * (self.slippage_bps / 10_000)
        comm = max(self.commission_per_share * qty, self.commission_min) if qty else 0.0
        reg = 0.0
        if is_sell:
            reg = notional * self.sec_fee_rate
            reg += min(self.finra_taf_per_share * qty, self.finra_taf_cap)
        return spread + slip + comm + reg

    def round_turn(self, price: float, qty: int) -> float:
        return self.entry_cost(price, qty) + self.exit_cost(price, qty)

    def round_turn_bps(self) -> float:
        """Approximate all-in round-turn cost in bps, ignoring per-share terms."""
        return 2 * (self.half_spread_bps + self.slippage_bps)


# Named scenarios used by the cost-sensitivity sweep in validate.py.
OPTIMISTIC = CostModel(half_spread_bps=0.3, slippage_bps=0.5)
BASE = CostModel(half_spread_bps=1.0, slippage_bps=2.0)
PESSIMISTIC = CostModel(half_spread_bps=2.0, slippage_bps=5.0)
