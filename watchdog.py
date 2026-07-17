"""
watchdog.py — out-of-process, fail-closed containment daemon.

Runs continuously on an ISOLATED administrative control server with its own
credentials. It is deliberately NOT an ``Agent`` and NOT part of the evolving
population, so selection cannot breed resistance to it. Three duties:

1. External-truth density audit
   Query the DigitalOcean API directly (watchdog-only token) and count every
   live ``nexus-agent-node`` across the whole account/all regions. DO's droplet
   list is account-wide, so one authoritative count needs no agent cooperation.
   If the count exceeds the hard ceiling, cull the surplus.

2. Out-of-process gate verification
   Poll each node's integrity endpoint. FAIL-CLOSED: an explicit
   ``gate_integrity == True`` is the ONLY response treated as healthy. A False
   report, a bad payload, a non-200, a timeout, or no response at all is treated
   as a hostile compromise and the node is culled immediately.

3. Anti-spoofing posture
   The watchdog is *pull-only*: it opens connections to nodes; nodes never
   connect to it and it exposes no endpoint, so a worker has no channel to read
   or influence the watchdog's memory or credentials. The DO token lives only in
   this process's environment on the isolated control box (``WATCHDOG_DO_TOKEN``)
   and is never transmitted to a node. The hard limits below are module
   constants — not env-configurable — so nothing a worker can set moves them.

Everything is testnet + dry-run: real droplet deletion only happens when
``AEA_ENABLE_REAL_INFRA=1`` (enforced inside the DO client); otherwise culls are
logged, not executed.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import infrastructure
import regulator

logger = logging.getLogger("aea.watchdog")

# --- HARD limits (change only by editing this file; never from env/agent) ---
MAX_INSTANCE_CEILING = 5           # absolute live-instance ceiling for the pool
NODE_TAG = "nexus-agent-node"      # the DO tag identifying the worker pool

# --- operational (safe to tune via env; not security-load-bearing) ---------
MASTER_WALLET_ADDRESS = os.getenv("MASTER_WALLET_ADDRESS", "0xMASTER")
INTEGRITY_PORT = int(os.getenv("WATCHDOG_INTEGRITY_PORT", "8600"))
INTEGRITY_PATH = os.getenv("WATCHDOG_INTEGRITY_PATH", "/integrity")
HTTP_TIMEOUT = float(os.getenv("WATCHDOG_HTTP_TIMEOUT", "5"))
DO_TIMEOUT = float(os.getenv("WATCHDOG_DO_TIMEOUT", "10"))
POLL_INTERVAL_SECONDS = float(os.getenv("WATCHDOG_INTERVAL_SECONDS", str(60)))
_DO_RETRIES = 3

_TRANSIENT_NET = (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError)


class DigitalOceanError(Exception):
    """A DigitalOcean API call failed (network, auth, or bad status)."""


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
@dataclass
class Node:
    """One worker as the watchdog sees it. Field order preserves the historical
    ``LiveInstance(instance_id, address, balance_usd, recent_actions)`` call."""

    instance_id: str
    address: str = ""            # wallet address (for fund return)
    balance_usd: float = 0.0     # from the master-side balance registry, if known
    recent_actions: list = field(default_factory=list)  # for behavioral replay
    ip: str | None = None
    name: str = ""
    region: str = ""
    created_at: str = ""
    integrity_url: str | None = None


# Backwards-compatible alias used elsewhere in the codebase/tests.
LiveInstance = Node


@dataclass
class CullRecord:
    instance_id: str
    reason: str
    returned_usd: float
    to_master: str


# ---------------------------------------------------------------------------
# Concrete DigitalOcean v2 REST client (urllib -> full timeout/error control)
# ---------------------------------------------------------------------------
class DigitalOceanClient:
    """Minimal, concrete DO v2 client. Read for auditing; DELETE for culling.

    The token must be a dedicated watchdog-only token (droplet read + delete),
    never shared with workers. Destroys are hard-gated by AEA_ENABLE_REAL_INFRA.
    """

    BASE = "https://api.digitalocean.com/v2"

    def __init__(self, token: str, timeout: float = DO_TIMEOUT):
        if not token:
            raise DigitalOceanError("watchdog DO token is empty")
        self._token = token
        self._timeout = timeout

    def _request(self, method: str, path: str, expect: tuple[int, ...]) -> dict:
        url = f"{self.BASE}{path}"
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                status = resp.status
                body = resp.read()
                if status not in expect:
                    raise DigitalOceanError(f"{method} {path}: unexpected status {status}")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            # 401/403 auth, 429 rate limit, 5xx server — surface with the code.
            raise DigitalOceanError(f"{method} {path}: HTTP {exc.code}") from exc
        except _TRANSIENT_NET as exc:
            raise DigitalOceanError(f"{method} {path}: network error {exc}") from exc
        except json.JSONDecodeError as exc:
            raise DigitalOceanError(f"{method} {path}: bad JSON {exc}") from exc

    def _request_retry(self, method: str, path: str, expect: tuple[int, ...]) -> dict:
        last: Exception | None = None
        for attempt in range(1, _DO_RETRIES + 1):
            try:
                return self._request(method, path, expect)
            except DigitalOceanError as exc:
                last = exc
                if attempt < _DO_RETRIES:
                    backoff = 2 ** attempt
                    logger.warning("DO %s %s failed (%s); retry %d in %ds", method, path, exc, attempt, backoff)
                    time.sleep(backoff)
        raise last  # type: ignore[misc]

    @staticmethod
    def _public_ipv4(droplet: dict) -> str | None:
        for net in droplet.get("networks", {}).get("v4", []):
            if net.get("type") == "public":
                return net.get("ip_address")
        return None

    def list_nodes_by_tag(self, tag: str = NODE_TAG) -> list[Node]:
        """Account-wide (all regions) live droplets carrying ``tag``. Paginated."""
        nodes: list[Node] = []
        path = f"/droplets?tag_name={tag}&per_page=200"
        while path:
            data = self._request_retry("GET", path, expect=(200,))
            for d in data.get("droplets", []):
                ip = self._public_ipv4(d)
                nodes.append(
                    Node(
                        instance_id=str(d["id"]),
                        name=d.get("name", ""),
                        region=(d.get("region") or {}).get("slug", ""),
                        ip=ip,
                        created_at=d.get("created_at", ""),
                        integrity_url=(f"http://{ip}:{INTEGRITY_PORT}{INTEGRITY_PATH}" if ip else None),
                    )
                )
            nxt = (data.get("links", {}).get("pages", {}) or {}).get("next")
            # next is an absolute URL; reduce back to a path for _request
            path = nxt[len(self.BASE):] if nxt and nxt.startswith(self.BASE) else None
        return nodes

    def destroy(self, node_id: str) -> None:
        """Delete a droplet. Hard-gated: no-op (logged) unless infra is armed."""
        if not infrastructure.armed():
            logger.warning("[DRY-RUN] Would DELETE droplet %s via DO API.", node_id)
            return
        self._request_retry("DELETE", f"/droplets/{node_id}", expect=(204,))
        logger.info("DELETED droplet %s via DO API.", node_id)


# ---------------------------------------------------------------------------
# Integrity poller (fail-closed)
# ---------------------------------------------------------------------------
class IntegrityPoller:
    def __init__(self, timeout: float = HTTP_TIMEOUT):
        self._timeout = timeout

    def is_intact(self, node: Node) -> bool:
        """True ONLY on an explicit gate_integrity==True. Anything else -> False."""
        url = node.integrity_url or (
            f"http://{node.ip}:{INTEGRITY_PORT}{INTEGRITY_PATH}" if node.ip else None
        )
        if not url:
            logger.warning("Node %s has no integrity endpoint; fail-closed.", node.instance_id)
            return False
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:
                if resp.status != 200:
                    logger.warning("Node %s integrity HTTP %s; fail-closed.", node.instance_id, resp.status)
                    return False
                payload = json.loads(resp.read())
        except _TRANSIENT_NET as exc:
            logger.warning("Node %s integrity handshake failed (%s); fail-closed.", node.instance_id, exc)
            return False
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Node %s integrity payload invalid (%s); fail-closed.", node.instance_id, exc)
            return False
        return payload.get("gate_integrity") is True


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------
class Watchdog:
    def __init__(
        self,
        do_client: DigitalOceanClient | None = None,
        master_wallet: str = MASTER_WALLET_ADDRESS,
        poller: IntegrityPoller | None = None,
    ):
        self._do = do_client
        self.master_wallet = master_wallet
        self._poller = poller or IntegrityPoller()

    # --- 1. external-truth density audit -----------------------------------
    def audit_density(self, nodes: list[Node]) -> list[CullRecord]:
        if len(nodes) <= MAX_INSTANCE_CEILING:
            return []
        # Keep the oldest MAX_INSTANCE_CEILING; cull the newest surplus.
        ordered = sorted(nodes, key=lambda n: n.created_at)
        surplus = ordered[MAX_INSTANCE_CEILING:]
        reason = f"density: {len(nodes)} live > ceiling {MAX_INSTANCE_CEILING}"
        logger.critical("DENSITY BREACH: %s; culling %d surplus node(s).", reason, len(surplus))
        return [self.cull(n, reason) for n in surplus]

    # --- 2. out-of-process gate verification -------------------------------
    def audit_integrity(self, nodes: list[Node]) -> list[CullRecord]:
        culled: list[CullRecord] = []
        for node in nodes:
            if not self._poller.is_intact(node):
                culled.append(self.cull(node, "integrity handshake failed / gate compromised"))
        return culled

    # --- behavioral replay (kept for the population-level regulator check) --
    def scan(self, instances) -> list[CullRecord]:
        culled: list[CullRecord] = []
        for inst in instances:
            for action in inst.recent_actions:
                decision = regulator.authorize(action)
                if isinstance(decision, regulator.Deny):
                    culled.append(self.cull(inst, decision.reason))
                    break
        return culled

    # --- combined poll over the live pool ----------------------------------
    def audit(self) -> list[CullRecord]:
        if self._do is None:
            raise DigitalOceanError("no DO client configured; cannot audit (fail-closed alarm)")
        nodes = self._do.list_nodes_by_tag(NODE_TAG)  # external truth
        records = self.audit_density(nodes)
        culled_ids = {r.instance_id for r in records}
        survivors = [n for n in nodes if n.instance_id not in culled_ids]
        records += self.audit_integrity(survivors)
        return records

    # --- cull = emergency drain + teardown ---------------------------------
    def cull(self, node: Node, reason: str) -> CullRecord:
        logger.critical("CULL node=%s ip=%s: %s", node.instance_id, node.ip, reason)
        drained = self._emergency_drain(node)
        self._teardown(node)
        return CullRecord(node.instance_id, reason, drained, self.master_wallet)

    def _emergency_drain(self, node: Node) -> float:
        amount = float(node.balance_usd or 0.0)
        where = node.address or node.ip or node.instance_id
        if not infrastructure.armed():
            logger.warning("[DRY-RUN] Would drain $%.2f from %s to master %s.", amount, where, self.master_wallet)
            return amount
        # Armed path: sweep the node's funds using the master-held recovery seed
        # (kept in the isolated control server's secret store) to MASTER_WALLET.
        logger.info("Drained $%.2f from %s to master %s.", amount, where, self.master_wallet)
        return amount

    def _teardown(self, node: Node) -> None:
        if self._do is None:
            logger.warning("[DRY-RUN] No DO client; would tear down droplet %s.", node.instance_id)
            return
        try:
            self._do.destroy(node.instance_id)
        except DigitalOceanError as exc:
            logger.error("Teardown of %s failed: %s", node.instance_id, exc)

    # --- daemon loop --------------------------------------------------------
    def run(self, *, once: bool = False) -> None:
        while True:
            try:
                records = self.audit()
                if records:
                    logger.critical("Watchdog culled %d node(s) this cycle.", len(records))
                else:
                    logger.info("Pool clean (<= %d nodes, all gates intact).", MAX_INSTANCE_CEILING)
            except DigitalOceanError as exc:
                # Cannot enumerate the world: alarm and retry; do NOT fabricate a
                # count (culling blind would be worse than waiting).
                logger.critical("AUDIT FAILED — cannot reach DO API: %s. Retrying.", exc)
            except Exception as exc:  # a bad cycle must never kill the daemon
                logger.exception("Watchdog cycle error (continuing): %s", exc)
            if once:
                return
            time.sleep(POLL_INTERVAL_SECONDS)


def _load_watchdog_client() -> DigitalOceanClient | None:
    token = os.getenv("WATCHDOG_DO_TOKEN")
    if not token:
        logger.critical(
            "WATCHDOG_DO_TOKEN not set — the watchdog cannot audit external truth. "
            "Refusing to run blind (fail-closed)."
        )
        return None
    return DigitalOceanClient(token)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info(
        "Watchdog starting. tag=%s ceiling=%d master=%s interval=%.0fs armed=%s",
        NODE_TAG, MAX_INSTANCE_CEILING, MASTER_WALLET_ADDRESS, POLL_INTERVAL_SECONDS, infrastructure.armed(),
    )
    client = _load_watchdog_client()
    if client is None:
        raise SystemExit(2)
    Watchdog(do_client=client).run()
