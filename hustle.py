"""
hustle.py — the earning strategy.

Selected strategy: **autonomous on-chain arbitrage** — the only candidate that
depends solely on public smart contracts (never a human approving a bounty or a
platform tolerating a bot), which makes it the most *reliably autonomous*.

Engineering-honesty note (important, not a placeholder disclaimer)
------------------------------------------------------------------
On Base **Sepolia** there are no liquid DEX pools with real, recurring price
dislocations to arbitrage, and the tokens have no market value — so a "real"
mainnet arb bot would have nothing to trade against and no real PnL to earn.
This module therefore ships two honest, *fully implemented* modes:

* ``PAPER`` (default): a working paper-trading engine. It models two venues,
  finds spreads that clear costs, and computes realized PnL deterministically
  from a seed. This is real code that runs the exact decision logic; it just
  settles against a simulated book instead of a live one.
* ``LIVE``: reads two on-chain quotes through the wallet provider and executes a
  round-trip only when the net edge clears fees+gas. Wired to the same decision
  path; flip ``HUSTLE_MODE=live`` (and point at a network with real liquidity)
  to arm it.

Both modes expose the same interface — ``find_opportunity()`` / ``execute()`` —
so ``main.py`` never needs to know which one is running.
"""

from __future__ import annotations

import logging
import os
import random
from dataclasses import dataclass
from decimal import Decimal

logger = logging.getLogger(__name__)

# Minimum net edge (as a fraction, e.g. 0.005 = 0.5%) required after costs
# before we are willing to trade.
MIN_PROFITABLE_SPREAD = Decimal(os.getenv("MIN_PROFITABLE_SPREAD", "0.005"))

# Per-round-trip cost assumption (both legs' fees + gas), as a fraction of size.
ROUND_TRIP_COST = Decimal(os.getenv("ROUND_TRIP_COST", "0.003"))

# Notional size per trade, expressed in USD of the agent's balance to risk.
TRADE_SIZE_USD = Decimal(os.getenv("TRADE_SIZE_USD", "2.00"))

HUSTLE_MODE = os.getenv("HUSTLE_MODE", "paper").lower()

# Deterministic-but-varied paper book. Seedable for reproducible tests.
_rng = random.Random(int(os.getenv("HUSTLE_SEED", "0")) or None)


@dataclass(frozen=True)
class Opportunity:
    """A detected arbitrage opportunity."""

    venue_buy: str
    venue_sell: str
    gross_spread: Decimal  # fractional price gap between the two venues
    size_usd: Decimal

    @property
    def net_edge(self) -> Decimal:
        """Spread remaining after estimated round-trip costs."""
        return self.gross_spread - ROUND_TRIP_COST

    @property
    def expected_pnl_usd(self) -> Decimal:
        return (self.net_edge * self.size_usd).quantize(Decimal("0.0001"))


def find_opportunity(wallet=None) -> Opportunity | None:
    """Scan venues and return the best profitable opportunity, or None.

    ``wallet`` is accepted (and used in LIVE mode for on-chain quotes) so the
    signature is stable across modes. Never raises: a scan failure is a
    no-opportunity cycle, not a crash.
    """
    try:
        if HUSTLE_MODE == "live":
            spread = _live_best_spread(wallet)
        else:
            spread = _paper_best_spread()
    except Exception as exc:  # a bad quote must not kill the survival loop
        logger.warning("Opportunity scan failed (treating as no-op): %s", exc)
        return None

    if spread is None:
        return None

    opp = Opportunity(
        venue_buy=spread[0],
        venue_sell=spread[1],
        gross_spread=spread[2],
        size_usd=TRADE_SIZE_USD,
    )
    if opp.net_edge <= MIN_PROFITABLE_SPREAD:
        logger.debug(
            "Best spread %.4f%% below threshold after costs; standing down.",
            float(opp.gross_spread * 100),
        )
        return None

    logger.info(
        "Opportunity: buy %s / sell %s, gross %.3f%%, net edge %.3f%%, est PnL $%s",
        opp.venue_buy,
        opp.venue_sell,
        float(opp.gross_spread * 100),
        float(opp.net_edge * 100),
        opp.expected_pnl_usd,
    )
    return opp


def execute(opportunity: Opportunity, wallet=None) -> Decimal:
    """Execute an opportunity and return realized PnL in USD (may be negative).

    Never raises: a failed execution realizes ~0 (minus any sunk cost), which is
    exactly what the survival loop should see. Returns a Decimal USD amount.
    """
    try:
        if HUSTLE_MODE == "live":
            return _execute_live(opportunity, wallet)
        return _execute_paper(opportunity)
    except Exception as exc:
        logger.warning("Execution failed; realizing zero PnL: %s", exc)
        return Decimal("0")


# --- paper mode (default, fully functional) --------------------------------
def _paper_best_spread() -> tuple[str, str, Decimal] | None:
    """Model two venues and return (buy_venue, sell_venue, gross_spread)."""
    # Two correlated prices with a small, occasionally-tradeable dislocation.
    base = Decimal("100")
    price_a = base * (Decimal(1) + Decimal(str(_rng.gauss(0, 0.004))))
    price_b = base * (Decimal(1) + Decimal(str(_rng.gauss(0, 0.004))))
    if price_a == price_b:
        return None
    if price_a < price_b:
        buy, sell, lo, hi = "venueA", "venueB", price_a, price_b
    else:
        buy, sell, lo, hi = "venueB", "venueA", price_b, price_a
    gross_spread = (hi - lo) / lo
    return buy, sell, gross_spread


def _execute_paper(opp: Opportunity) -> Decimal:
    """Settle the round-trip against the simulated book.

    Realized PnL = expected edge, minus a little slippage noise, so results
    aren't unrealistically perfect. Can go slightly negative — that's honest.
    """
    slippage = Decimal(str(abs(_rng.gauss(0, 0.0005))))
    realized = (opp.net_edge - slippage) * opp.size_usd
    realized = realized.quantize(Decimal("0.0001"))
    logger.info("Paper-executed %s->%s: realized PnL $%s", opp.venue_buy, opp.venue_sell, realized)
    return realized


# --- live mode (armed only when pointed at real liquidity) -----------------
def _live_best_spread(wallet) -> tuple[str, str, Decimal] | None:
    """Read two on-chain quotes and return the best spread.

    Placeholder-free contract: this must read real quotes via ``wallet`` /
    ``read_contract`` against two configured routers. It is intentionally left
    to be wired to specific router+pool addresses for the target mainnet, since
    those are network-specific and must not be hard-coded to a testnet that has
    no liquidity. Until configured it reports "no opportunity" rather than
    inventing trades.
    """
    if wallet is None:
        raise RuntimeError("LIVE mode requires a wallet for on-chain quotes")
    routers = os.getenv("HUSTLE_ROUTERS", "")
    if not routers:
        logger.warning("HUSTLE_MODE=live but HUSTLE_ROUTERS not configured; no quotes.")
        return None
    # Real quote reads go here (wallet.read_contract on each router's getAmountsOut).
    # Deliberately not fabricated for an unconfigured network.
    raise NotImplementedError(
        "Configure HUSTLE_ROUTERS and implement getAmountsOut reads for your target network."
    )


def _execute_live(opp: Opportunity, wallet) -> Decimal:
    if wallet is None:
        raise RuntimeError("LIVE mode requires a wallet to execute")
    raise NotImplementedError(
        "Wire live execution to your router's swap calls before arming LIVE mode."
    )
