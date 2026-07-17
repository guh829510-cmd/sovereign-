"""
main.py — the survival loop for the Autonomous Economic Agent (AEA).

This is the orchestrator. It wires together the three subsystems:

    wallet.py          -> read balance, hold funds, pay bills
    hustle.py          -> the earning strategy (testnet DEX arbitrage loop)
    infrastructure.py  -> self-termination and self-cloning

Survival rules (from the project brief):
    * Start with ~$10 of value in a Base Sepolia wallet.
    * If balance < $1.00  -> terminate this server (we can no longer pay bills).
    * If balance > $20.00 -> clone to a fresh server, then keep running.

The loop is intentionally boring and defensive: check balance, act, sleep,
repeat. Nothing here should ever raise an unhandled exception and kill the
process silently — an AEA that crashes is an AEA that dies.
"""

from __future__ import annotations

# Thresholds are the heart of the agent's "biology". Kept here so they are
# trivial to tune and to unit-test.
TERMINATE_BELOW_USD = 1.00
CLONE_ABOVE_USD = 20.00
LOOP_INTERVAL_SECONDS = 60


def run() -> None:
    """Main survival loop. Implemented in Step 3."""
    raise NotImplementedError("Wire up wallet + hustle + infrastructure here.")


if __name__ == "__main__":
    run()
