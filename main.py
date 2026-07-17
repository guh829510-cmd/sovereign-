"""
main.py — the survival loop (heartbeat) for the Autonomous Economic Agent.

Every ``HEARTBEAT_SECONDS`` (default 60 minutes) the agent wakes, reads its
balance, and takes exactly one decision:

    balance < $1   -> STARVING: log a warning and destroy_self()
    balance > $20  -> REPRODUCE: mint a child wallet, send it $10, clone_self()
    otherwise      -> HUSTLE: run one earning cycle

Design promises:
* The loop is a ``while True`` heartbeat that never dies from an operational
  error — every cycle is wrapped so a bad RPC read or a failed trade just means
  "try again next heartbeat".
* We never act on an *unknown* balance. If it can't be read, we skip the cycle
  rather than risk self-terminating over a transient network error.
* Everything is logged to stdout AND a rotating file so the operator can audit
  the agent's financial state and every decision it made.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import time
from decimal import Decimal

import hustle
import infrastructure
from wallet import BalanceUnavailable, TransferError, Wallet, WalletError

logger = logging.getLogger("aea.main")

# Survival thresholds — the heart of the agent's "biology".
STARVATION_USD = float(os.getenv("TERMINATE_BELOW_USD", "1.00"))
REPRODUCTION_USD = float(os.getenv("CLONE_ABOVE_USD", "20.00"))
CLONE_STAKE_USD = float(os.getenv("CLONE_STAKE_USD", "10.00"))
HEARTBEAT_SECONDS = float(os.getenv("LOOP_INTERVAL_SECONDS", str(60 * 60)))

LOG_FILE = os.getenv("AEA_LOG_FILE", "aea.log")


def setup_logging() -> None:
    """Configure comprehensive logging to both stdout and a rotating file."""
    root = logging.getLogger()
    root.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5
    )
    file_handler.setFormatter(fmt)

    root.handlers.clear()
    root.addHandler(stream)
    root.addHandler(file_handler)


def _starve() -> None:
    """Terminate this server; the agent can no longer pay its bills."""
    logger.warning("STARVING: balance below $%.2f. Executing self-destruction.", STARVATION_USD)
    try:
        infrastructure.destroy_self()
    except infrastructure.InfrastructureError as exc:
        logger.error("Self-destruction failed: %s", exc)


def _reproduce(wallet: Wallet) -> None:
    """Mint a child wallet, fund it with the stake, and clone to a new server.

    Order matters: we fund the child BEFORE cloning so we never spin up an
    unfunded (instantly-starving) clone. Any failure here is non-fatal — the
    parent simply keeps living and hustling.
    """
    logger.info("REPRODUCING: balance above $%.2f.", REPRODUCTION_USD)
    try:
        child = Wallet.new_child()
    except WalletError as exc:
        logger.error("Could not mint child wallet; skipping reproduction: %s", exc)
        return
    logger.info("Minted child wallet %s. Funding with $%.2f.", child.address, CLONE_STAKE_USD)

    try:
        tx = wallet.transfer_usd(child.address, CLONE_STAKE_USD)
    except TransferError as exc:
        logger.error("Funding transfer failed; aborting reproduction (no clone): %s", exc)
        return
    logger.info("Funded child (tx %s). Cloning to new server.", tx)

    try:
        child_id = infrastructure.clone_self(child, starting_stake_usd=CLONE_STAKE_USD)
        logger.info("Reproduction complete. Child instance: %s", child_id)
    except infrastructure.InfrastructureError as exc:
        logger.error("Clone failed after funding child %s: %s", child.address, exc)


def heartbeat(wallet: Wallet) -> bool:
    """Run one heartbeat. Returns False if the agent has terminated itself."""
    try:
        balance = wallet.usd_balance()
    except BalanceUnavailable as exc:
        logger.warning("Balance unavailable this heartbeat; skipping actions: %s", exc)
        return True

    logger.info("Heartbeat. Balance: ~$%.2f | Session PnL: $%s", balance, hustle.session_pnl())

    if balance < STARVATION_USD:
        _starve()
        return False

    if balance > REPRODUCTION_USD:
        _reproduce(wallet)
        return True

    pnl = hustle.run_cycle(wallet)
    logger.info("Hustle heartbeat done. Realized this cycle: $%s", pnl)
    return True


def run() -> None:
    setup_logging()
    logger.info(
        "AEA booting. mode=%s heartbeat=%.0fs starve<$%.2f reproduce>$%.2f",
        hustle.HUSTLE_MODE,
        HEARTBEAT_SECONDS,
        STARVATION_USD,
        REPRODUCTION_USD,
    )

    try:
        wallet = Wallet()
    except WalletError as exc:
        logger.critical("Wallet init failed; cannot run: %s", exc)
        raise SystemExit(1) from exc
    logger.info("Wallet %s online.", wallet.address)

    while True:
        try:
            alive = heartbeat(wallet)
        except KeyboardInterrupt:
            logger.info("Interrupted by operator; shutting down.")
            break
        except Exception as exc:  # last-resort net so the heartbeat never dies
            logger.exception("Unexpected error in heartbeat (continuing): %s", exc)
            alive = True

        if not alive:
            logger.info("Agent has terminated itself. Heartbeat stopped.")
            break

        logger.info("Sleeping %.0fs until next heartbeat.", HEARTBEAT_SECONDS)
        time.sleep(HEARTBEAT_SECONDS)


if __name__ == "__main__":
    run()
