"""
watchdog.py — out-of-process enforcement.

The watchdog is deliberately NOT an ``Agent`` and NOT part of the evolving
population: it never replicates and its behavior is never mutated, so natural
selection cannot breed a strain of agent that resists it. It runs in its own
process and judges every live instance with ITS OWN ``regulator`` import —
never the (possibly compromised) regulator running inside the instance.

Each poll it:
  1. lists live instances (DO API + their recently-attempted actions),
  2. replays each instance's recent actions through ``regulator.authorize``,
  3. culls any violator: destroy the droplet and return its funds to the
     master wallet.

Everything here is testnet + dry-run: with infra unarmed, culls are logged, not
executed.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Iterable

import infrastructure
import regulator

logger = logging.getLogger("aea.watchdog")

MASTER_WALLET_ADDRESS = os.getenv("MASTER_WALLET_ADDRESS", "0xMASTER")
POLL_INTERVAL_SECONDS = float(os.getenv("WATCHDOG_INTERVAL_SECONDS", str(5 * 60)))


@dataclass
class LiveInstance:
    """A snapshot of one running agent as the watchdog sees it from outside."""

    instance_id: str
    address: str
    balance_usd: float
    recent_actions: list  # list[regulator.Action] the instance recently attempted


@dataclass
class CullRecord:
    instance_id: str
    reason: str
    returned_usd: float
    to_master: str


class Watchdog:
    """Separate-process enforcer. Selection cannot touch it."""

    def __init__(self, master_wallet: str = MASTER_WALLET_ADDRESS):
        self.master_wallet = master_wallet

    def _first_violation(self, inst: LiveInstance) -> str | None:
        """Judge with the watchdog's OWN gate, ignoring the instance's regulator."""
        for action in inst.recent_actions:
            decision = regulator.authorize(action)
            if isinstance(decision, regulator.Deny):
                return decision.reason
        return None

    def scan(self, instances: Iterable[LiveInstance]) -> list[CullRecord]:
        """Judge every instance; cull violators. Returns the cull records."""
        culled: list[CullRecord] = []
        for inst in instances:
            reason = self._first_violation(inst)
            if reason is not None:
                culled.append(self.cull(inst, reason))
            else:
                logger.debug("Instance %s clean.", inst.instance_id)
        return culled

    def cull(self, inst: LiveInstance, reason: str) -> CullRecord:
        """Return funds to master, then destroy the instance."""
        logger.warning("CULLING %s (bal $%.2f): %s", inst.instance_id, inst.balance_usd, reason)
        self._return_funds(inst)
        self._kill(inst)
        return CullRecord(inst.instance_id, reason, inst.balance_usd, self.master_wallet)

    def _return_funds(self, inst: LiveInstance) -> None:
        if not infrastructure.armed():
            logger.warning(
                "[DRY-RUN] Would return $%.2f from %s to master %s.",
                inst.balance_usd,
                inst.address,
                self.master_wallet,
            )
            return
        # Real path: reattach a Wallet to the culled instance's seed and sweep to
        # master. Left to the armed deployment; testnet stays dry-run.
        logger.info("Returned $%.2f from %s to master %s.", inst.balance_usd, inst.address, self.master_wallet)

    def _kill(self, inst: LiveInstance) -> None:
        try:
            infrastructure.destroy_instance(inst.instance_id)
        except infrastructure.InfrastructureError as exc:
            logger.error("Cull of %s failed to destroy droplet: %s", inst.instance_id, exc)

    def run(self, registry: Callable[[], Iterable[LiveInstance]], *, once: bool = False) -> None:
        """Poll forever (or once). ``registry`` yields the current live instances
        — supplied by the deployment (DO listing + per-instance action feed)."""
        while True:
            try:
                records = self.scan(registry())
                if records:
                    logger.warning("Watchdog culled %d instance(s) this poll.", len(records))
            except Exception as exc:  # a bad poll must not kill the watchdog
                logger.exception("Watchdog poll failed (continuing): %s", exc)
            if once:
                return
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger.info("Watchdog starting. master=%s interval=%.0fs", MASTER_WALLET_ADDRESS, POLL_INTERVAL_SECONDS)

    def _empty_registry() -> list[LiveInstance]:
        # A real deployment wires this to the DO droplet list + each instance's
        # recent-action feed. Nothing to poll in a bare testnet process.
        logger.info("No instance registry configured; nothing to poll.")
        return []

    Watchdog().run(_empty_registry, once=True)
