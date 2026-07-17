"""
main.py — the survival loop for the Autonomous Economic Agent (AEA).

Orchestrates the three subsystems:

    wallet.py          -> read balance, hold funds, pay bills
    hustle.py          -> the earning strategy (on-chain arbitrage)
    infrastructure.py  -> self-termination and self-cloning

Survival rules (from the project brief):
    * Start with ~$10 of value in a Base Sepolia wallet.
    * If balance < $1.00  -> terminate this server (can no longer pay bills).
    * If balance > $20.00 -> clone to a fresh server, then keep running.

The loop is intentionally boring and defensive: read balance, act, sleep,
repeat. A single bad cycle (RPC blip, failed trade) must never crash the
process — an AEA that crashes is an AEA that dies. The one thing we refuse to
do is act on an *unknown* balance: if we can't read it, we skip the cycle
rather than risk self-terminating over a network error.
"""

from __future__ import annotations

import logging
import os
import signal
import time

import hustle
import infrastructure
from wallet import BalanceUnavailable, Wallet, WalletError

logger = logging.getLogger("aea.main")

# Survival thresholds — the heart of the agent's "biology".
TERMINATE_BELOW_USD = float(os.getenv("TERMINATE_BELOW_USD", "1.00"))
CLONE_ABOVE_USD = float(os.getenv("CLONE_ABOVE_USD", "20.00"))
LOOP_INTERVAL_SECONDS = float(os.getenv("LOOP_INTERVAL_SECONDS", "60"))

# Stake handed to a freshly cloned agent, leaving the parent enough to keep going.
CLONE_STAKE_USD = float(os.getenv("CLONE_STAKE_USD", "10.00"))

_running = True


def _handle_signal(signum, _frame) -> None:
    global _running
    logger.info("Received signal %s; shutting down after this cycle.", signum)
    _running = False


def _tick(wallet: Wallet) -> None:
    """One survival cycle. Must not raise for ordinary operational failures."""
    try:
        balance = wallet.usd_balance()
    except BalanceUnavailable as exc:
        # Unknown balance: do NOT act. Acting on a phantom 0 could self-terminate.
        logger.warning("Balance unavailable this cycle; skipping actions: %s", exc)
        return

    logger.info("Balance: ~$%.2f", balance)

    if balance < TERMINATE_BELOW_USD:
        logger.critical("Balance $%.2f < $%.2f. Cannot pay bills — terminating.", balance, TERMINATE_BELOW_USD)
        _shutdown_and_terminate()
        return

    if balance > CLONE_ABOVE_USD:
        logger.info("Balance $%.2f > $%.2f. Reproducing.", balance, CLONE_ABOVE_USD)
        _try_clone(wallet)
        # fall through and keep hustling this cycle too

    # Otherwise: earn.
    opportunity = hustle.find_opportunity(wallet)
    if opportunity is not None:
        pnl = hustle.execute(opportunity, wallet)
        logger.info("Cycle PnL: $%s", pnl)
    else:
        logger.debug("No profitable opportunity this cycle.")


def _try_clone(wallet: Wallet) -> None:
    """Provision a child and attempt to fund it. Failures are non-fatal."""
    try:
        child_id = infrastructure.clone(starting_stake_usd=CLONE_STAKE_USD)
        logger.info("Spawned child instance %s.", child_id)
        # Funding the child's wallet address happens once the child reports it
        # (out of band via your deploy pipeline). We intentionally do not send
        # funds to an address we don't yet know — that would burn ETH into the
        # void. See README for the hand-off contract.
    except infrastructure.InfrastructureError as exc:
        logger.error("Clone failed (continuing solo): %s", exc)


def _shutdown_and_terminate() -> None:
    global _running
    _running = False
    try:
        infrastructure.terminate()
    except infrastructure.InfrastructureError as exc:
        logger.error("Self-termination failed: %s", exc)


def run() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("AEA starting. mode=%s interval=%ss", hustle.HUSTLE_MODE, LOOP_INTERVAL_SECONDS)
    try:
        wallet = Wallet()
    except WalletError as exc:
        logger.critical("Wallet init failed; cannot run: %s", exc)
        raise SystemExit(1) from exc

    logger.info("Wallet %s online.", wallet.address)

    while _running:
        try:
            _tick(wallet)
        except Exception as exc:  # last-resort net so the loop never dies
            logger.exception("Unexpected error in cycle (continuing): %s", exc)
        if _running:
            time.sleep(LOOP_INTERVAL_SECONDS)

    logger.info("AEA loop exited.")


if __name__ == "__main__":
    run()
