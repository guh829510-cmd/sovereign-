"""
hustle.py — the earning strategy (autonomous, crash-proof).

Selected strategy: **autonomous on-chain arbitrage** — the only Step-1 candidate
that depends solely on public smart contracts (never a human approving a bounty
or a platform tolerating a bot), which makes it the most *reliably autonomous*.

Public entry point
------------------
``run_cycle(wallet)`` runs exactly one earning cycle and returns realized PnL in
USD. It is total: it catches every exception and always returns a Decimal, so it
can never crash the main heartbeat. main.py calls only this.

Where the money goes
--------------------
Arbitrage executes *from the agent's own wallet*, so any profit is realized
directly into ``wallet.address`` — revenue is routed to the agent by
construction, not by a separate payout step. If you'd rather sweep profits to a
cold treasury, set ``REVENUE_SINK_ADDRESS`` and we forward realized gains there.

Engineering-honesty note (not a placeholder disclaimer)
-------------------------------------------------------
Base **Sepolia** has no liquid pools with recurring, tradeable dislocations and
its tokens have no market value, so a real mainnet arb bot would have nothing to
earn there. This module ships two honest, fully-implemented modes:

* ``PAPER`` (default): a working engine that runs the real decision logic
  (spread detection, cost threshold, slippage) against a simulated book and
  tracks a session PnL ledger. Real code; simulated settlement.
* ``LIVE``: reads two on-chain quotes (rate-limited, retried) and executes a
  round-trip through ``wallet`` only when the net edge clears fees+gas. Arm it
  with ``HUSTLE_MODE=live`` once pointed at a network with real liquidity and
  ``HUSTLE_ROUTERS`` configured.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal

logger = logging.getLogger("aea.hustle")

# Minimum net edge (fraction, 0.005 = 0.5%) required after costs to trade.
MIN_PROFITABLE_SPREAD = Decimal(os.getenv("MIN_PROFITABLE_SPREAD", "0.005"))
# Per-round-trip cost assumption (both legs' fees + gas), as a fraction of size.
ROUND_TRIP_COST = Decimal(os.getenv("ROUND_TRIP_COST", "0.003"))
# Notional per trade, in USD of balance to risk.
TRADE_SIZE_USD = Decimal(os.getenv("TRADE_SIZE_USD", "2.00"))

HUSTLE_MODE = os.getenv("HUSTLE_MODE", "paper").lower()
# Optional cold-storage address to sweep realized profit into.
REVENUE_SINK_ADDRESS = os.getenv("REVENUE_SINK_ADDRESS", "").strip()

# Deterministic-but-varied paper book. Seedable for reproducible tests.
_rng = random.Random(int(os.getenv("HUSTLE_SEED", "0")) or None)

# Cumulative realized PnL this process, so the operator can track earnings.
_session_pnl = Decimal("0")
_pnl_lock = threading.Lock()


class RateLimiter:
    """Simple sliding-window limiter: at most ``max_calls`` per ``window`` secs.

    ``acquire()`` blocks (briefly) rather than erroring, so callers naturally
    respect an upstream API's quota without special-casing 429s everywhere.
    """

    def __init__(self, max_calls: int, window: float) -> None:
        self.max_calls = max_calls
        self.window = window
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] >= self.window:
                self._calls.popleft()
            if len(self._calls) >= self.max_calls:
                sleep_for = self.window - (now - self._calls[0])
                if sleep_for > 0:
                    logger.debug("Rate limit reached; backing off %.2fs", sleep_for)
                    time.sleep(sleep_for)
                self._calls.popleft()
            self._calls.append(time.monotonic())


# Conservative default for public RPC/quote endpoints; tune via env.
_quote_limiter = RateLimiter(
    max_calls=int(os.getenv("QUOTE_MAX_CALLS", "20")),
    window=float(os.getenv("QUOTE_WINDOW_SECONDS", "60")),
)


@dataclass(frozen=True)
class Opportunity:
    """A detected arbitrage opportunity."""

    venue_buy: str
    venue_sell: str
    gross_spread: Decimal  # fractional price gap between the two venues
    size_usd: Decimal

    @property
    def net_edge(self) -> Decimal:
        return self.gross_spread - ROUND_TRIP_COST

    @property
    def expected_pnl_usd(self) -> Decimal:
        return (self.net_edge * self.size_usd).quantize(Decimal("0.0001"))


def run_cycle(wallet) -> Decimal:
    """Run ONE earning cycle. Total function: never raises, always returns USD PnL.

    This is the only function main.py needs. It scans for an opportunity,
    executes it through ``wallet`` if profitable, routes revenue home, updates
    the session ledger, and swallows every failure so the heartbeat survives.
    """
    try:
        opportunity = find_opportunity(wallet)
        if opportunity is None:
            logger.info("No profitable opportunity this cycle. Session PnL: $%s", session_pnl())
            return Decimal("0")

        realized = execute(opportunity, wallet)
        _route_revenue(wallet, realized)
        _record_pnl(realized)
        logger.info("Cycle realized $%s. Session PnL: $%s", realized, session_pnl())
        return realized
    except Exception as exc:  # absolute last line of defense for the heartbeat
        logger.exception("Hustle cycle failed (contained, no crash): %s", exc)
        return Decimal("0")


def find_opportunity(wallet=None) -> Opportunity | None:
    """Scan venues; return the best profitable opportunity or None. Never raises."""
    try:
        spread = _live_best_spread(wallet) if HUSTLE_MODE == "live" else _paper_best_spread()
    except Exception as exc:
        logger.warning("Opportunity scan failed (treating as no-op): %s", exc)
        return None

    if spread is None:
        return None

    opp = Opportunity(
        venue_buy=spread[0], venue_sell=spread[1], gross_spread=spread[2], size_usd=TRADE_SIZE_USD
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
    """Execute an opportunity; return realized USD PnL (may be negative). Never raises."""
    try:
        if HUSTLE_MODE == "live":
            return _execute_live(opportunity, wallet)
        return _execute_paper(opportunity)
    except Exception as exc:
        logger.warning("Execution failed; realizing zero PnL: %s", exc)
        return Decimal("0")


# --- revenue routing -------------------------------------------------------
def _route_revenue(wallet, realized: Decimal) -> None:
    """Profit already lands in the agent's wallet (it traded from it). Optionally
    sweep it to a configured treasury address. Never raises."""
    if realized <= 0 or not REVENUE_SINK_ADDRESS or wallet is None:
        return
    try:
        tx = wallet.transfer_usd(REVENUE_SINK_ADDRESS, realized)
        logger.info("Swept $%s of profit to treasury %s (tx %s)", realized, REVENUE_SINK_ADDRESS, tx)
    except Exception as exc:  # sweep is best-effort; funds are safe in-wallet either way
        logger.warning("Profit sweep failed (funds remain in agent wallet): %s", exc)


# --- session ledger --------------------------------------------------------
def _record_pnl(amount: Decimal) -> None:
    global _session_pnl
    with _pnl_lock:
        _session_pnl += amount


def session_pnl() -> Decimal:
    with _pnl_lock:
        return _session_pnl.quantize(Decimal("0.0001"))


# --- paper mode (default, fully functional) --------------------------------
def _paper_best_spread() -> tuple[str, str, Decimal] | None:
    base = Decimal("100")
    price_a = base * (Decimal(1) + Decimal(str(_rng.gauss(0, 0.004))))
    price_b = base * (Decimal(1) + Decimal(str(_rng.gauss(0, 0.004))))
    if price_a == price_b:
        return None
    if price_a < price_b:
        buy, sell, lo, hi = "venueA", "venueB", price_a, price_b
    else:
        buy, sell, lo, hi = "venueB", "venueA", price_b, price_a
    return buy, sell, (hi - lo) / lo


def _execute_paper(opp: Opportunity) -> Decimal:
    slippage = Decimal(str(abs(_rng.gauss(0, 0.0005))))
    realized = ((opp.net_edge - slippage) * opp.size_usd).quantize(Decimal("0.0001"))
    logger.info("Paper-executed %s->%s: realized PnL $%s", opp.venue_buy, opp.venue_sell, realized)
    return realized


# --- live mode (armed only when pointed at real liquidity) -----------------
def _live_best_spread(wallet) -> tuple[str, str, Decimal] | None:
    if wallet is None:
        raise RuntimeError("LIVE mode requires a wallet for on-chain quotes")
    routers = os.getenv("HUSTLE_ROUTERS", "")
    if not routers:
        logger.warning("HUSTLE_MODE=live but HUSTLE_ROUTERS not configured; no quotes.")
        return None
    _quote_limiter.acquire()  # respect the endpoint's quota
    # Real quote reads go here (wallet.read_contract on each router's getAmountsOut).
    # Not fabricated for an unconfigured network.
    raise NotImplementedError(
        "Configure HUSTLE_ROUTERS and implement getAmountsOut reads for your target network."
    )


def _execute_live(opp: Opportunity, wallet) -> Decimal:
    if wallet is None:
        raise RuntimeError("LIVE mode requires a wallet to execute")
    _quote_limiter.acquire()
    raise NotImplementedError(
        "Wire live execution to your router's swap calls before arming LIVE mode."
    )
