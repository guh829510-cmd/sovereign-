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
from pathlib import Path

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
# Where child wallet seeds are persisted before funding. Files end in
# ``_seed.json`` which is git-ignored, so they never get committed.
CHILD_SEED_DIR = os.getenv("CHILD_SEED_DIR", ".")


def _child_seed_path(address: str) -> Path:
    return Path(CHILD_SEED_DIR) / f"child_{address}_seed.json"


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
    """Mint a child wallet, fund it, and clone to a new server — safely ordered.

    Ordering is deliberate and load-bearing (fixes a fund-loss bug):
      1. Check the instance cap BEFORE any funds move; bail if at cap.
      2. Persist the child seed durably (0600, git-ignored) BEFORE any transfer,
         so the child is recoverable even if a later step fails.
      3. Gate the funding transfer behind the same arm flag as real infra; in
         dry-run we log the intended transfer but never move real money.
      4. Only after cap-check, seed-persist, and (if armed) funding do we clone.

    Every failure is non-fatal — the parent keeps living and hustling.
    """
    logger.info("REPRODUCING: balance above $%.2f.", REPRODUCTION_USD)

    # 1. Cap check — before minting or moving anything.
    try:
        if not infrastructure.can_clone():
            logger.warning(
                "At instance cap (%d); skipping reproduction. No funds moved.",
                infrastructure.MAX_LIVING_INSTANCES,
            )
            return
    except infrastructure.InfrastructureError as exc:
        logger.error("Could not verify instance cap; skipping reproduction (no funds moved): %s", exc)
        return

    # Mint the child.
    try:
        child = Wallet.new_child()
    except WalletError as exc:
        logger.error("Could not mint child wallet; skipping reproduction: %s", exc)
        return
    logger.info("Minted child wallet %s.", child.address)

    # 2. Persist the child seed BEFORE any money can reach it.
    try:
        seed_path = child.persist_seed_to(_child_seed_path(child.address))
    except WalletError as exc:
        logger.error(
            "Could not persist child seed; aborting reproduction (no funds moved): %s", exc
        )
        return
    logger.info("Persisted child seed to %s (recoverable before funding).", seed_path)

    # 3. Fund — only when armed; otherwise log intent and move on.
    if infrastructure.armed():
        try:
            tx = wallet.transfer_usd(child.address, CLONE_STAKE_USD)
        except TransferError as exc:
            logger.error(
                "Funding transfer failed; aborting reproduction. Child seed at %s "
                "is recoverable: %s",
                seed_path,
                exc,
            )
            return
        logger.info("Funded child %s with $%.2f (tx %s).", child.address, CLONE_STAKE_USD, tx)
    else:
        logger.warning(
            "[DRY-RUN] Would fund child %s with $%.2f. Set AEA_ENABLE_REAL_INFRA=1 "
            "to move real funds.",
            child.address,
            CLONE_STAKE_USD,
        )

    # 4. Clone — last, after cap-check, seed-persist, and (if armed) funding.
    try:
        child_id = infrastructure.clone_self(child, starting_stake_usd=CLONE_STAKE_USD)
        logger.info("Reproduction complete. Child instance: %s", child_id)
    except infrastructure.InfrastructureError as exc:
        logger.error("Clone failed. Child seed at %s is recoverable: %s", seed_path, exc)


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
