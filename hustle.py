"""
hustle.py — the earning strategy.

Selected strategy: **autonomous testnet DEX arbitrage** (see the analysis in
the project notes / commit message). Rationale in one line: it is fully
self-contained — it depends only on public smart contracts, never on a human
approving a bounty or a platform not banning us — which makes it the most
*reliably autonomous* of the candidate hustles.

The loop:
    1. Read the price of an asset across two on-chain venues (e.g. two pools).
    2. If the spread exceeds fees + gas + a safety margin, execute the round-trip.
    3. Record realized PnL so the survival loop can react to it.

IMPORTANT (engineering honesty): on Base *Sepolia* the tokens are faucet
tokens with no market value, so realized "profit" is not real income. This
module is therefore built as an honest simulation/scaffold of the mainnet
strategy — same interface, same risk checks — so it can be pointed at a real
network later if and only if a human decides to fund it with real capital.
"""

from __future__ import annotations

# Minimum edge (in %) required before we're willing to trade, after costs.
MIN_PROFITABLE_SPREAD_PCT = 0.5


def find_opportunity() -> dict | None:
    """Scan venues; return the best arb opportunity, or None. Step 3."""
    raise NotImplementedError


def execute(opportunity: dict) -> float:
    """Execute an opportunity; return realized PnL in USD. Step 3."""
    raise NotImplementedError
