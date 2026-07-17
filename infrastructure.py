"""
infrastructure.py — the agent's body: birth and death of servers.

Uses ``python-digitalocean`` to control the compute the agent runs on:

    terminate()  -> destroy this droplet when the agent can no longer pay its
                    bills (balance < $1). The agent "dying".
    clone()      -> provision a NEW droplet that boots this same agent, when the
                    agent is wealthy (balance > $20). The agent "reproducing".

Safety model (this file spends real money and can self-replicate)
-----------------------------------------------------------------
Two independent guards stand between this code and a surprise invoice:

1. **Dry-run by default.** Nothing touches DigitalOcean unless
   ``AEA_ENABLE_REAL_INFRA=1`` is explicitly set. Otherwise every operation
   logs exactly what it *would* do and returns a synthetic id. You cannot
   fork-bomb real servers by accident just by running the agent.
2. **Hard instance cap.** ``clone()`` counts live agent droplets (by tag) and
   refuses to exceed :data:`MAX_LIVING_INSTANCES`, so even armed, a runaway
   loop is bounded.

``terminate()`` is deliberately the last thing the process does; callers should
flush wallet/ledger state before invoking it.
"""

from __future__ import annotations

import logging
import os
import urllib.request
import uuid

logger = logging.getLogger(__name__)

# --- guard rails -----------------------------------------------------------
MAX_LIVING_INSTANCES = int(os.getenv("MAX_LIVING_INSTANCES", "3"))
AEA_TAG = os.getenv("AEA_TAG", "aea-agent")  # tag used to find sibling droplets
DROPLET_REGION = os.getenv("DROPLET_REGION", "nyc1")
DROPLET_SIZE = os.getenv("DROPLET_SIZE", "s-1vcpu-1gb")
DROPLET_IMAGE = os.getenv("DROPLET_IMAGE", "ubuntu-22-04-x64")

# The repo the clone should boot from (public URL or one with an embedded token
# handled by your deploy pipeline — do NOT bake secrets into user_data).
AEA_REPO_URL = os.getenv("AEA_REPO_URL", "")

_METADATA_ID_URL = "http://169.254.169.254/metadata/v1/id"


class InfrastructureError(Exception):
    """Raised when a real infrastructure operation cannot be completed."""


def _real_infra_enabled() -> bool:
    return os.getenv("AEA_ENABLE_REAL_INFRA", "").lower() in {"1", "true", "yes"}


def armed() -> bool:
    """Public predicate: are real, money/server-moving actions enabled?

    The survival loop uses this to gate the child-funding transfer with the same
    flag that gates real provisioning, so no real funds move while infra is dry.
    """
    return _real_infra_enabled()


def can_clone() -> bool:
    """True if we are below the instance cap and may provision another agent.

    Callable *before* any funds move, so reproduction can bail without stranding
    a stake. In dry-run there are no real droplets to count (and no token), so
    this returns True; the authoritative live count only runs when armed —
    ``clone_self`` re-checks the cap as defense in depth.
    """
    if not _real_infra_enabled():
        return True
    import digitalocean

    token = _require_token()
    manager = digitalocean.Manager(token=token)
    return _living_instance_count(manager) < MAX_LIVING_INSTANCES


def _require_token() -> str:
    token = os.getenv("DIGITALOCEAN_TOKEN") or os.getenv("DIGITALOCEAN_ACCESS_TOKEN")
    if not token:
        raise InfrastructureError(
            "DIGITALOCEAN_TOKEN is required to manage infrastructure."
        )
    return token


def _current_droplet_id() -> str | None:
    """Best-effort self-identification: env override, then DO metadata service."""
    env_id = os.getenv("DROPLET_ID")
    if env_id:
        return env_id
    try:
        with urllib.request.urlopen(_METADATA_ID_URL, timeout=2) as resp:
            return resp.read().decode().strip() or None
    except Exception as exc:  # not on a droplet, or metadata unreachable
        logger.debug("Could not read droplet id from metadata: %s", exc)
        return None


def _cloud_init(child_wallet_data: str | None) -> str:
    """user_data script that boots a fresh agent.

    The child's *wallet seed* is delivered here (base64) so the clone controls
    the funds the parent sends it. NOTE: user_data is readable by anything on the
    droplet and stored by DigitalOcean — acceptable for a valueless testnet seed,
    but on mainnet deliver the seed via a secrets manager instead, not cloud-init.
    CDP API keys are NOT embedded; supply them out of band.
    """
    repo = AEA_REPO_URL or "<set AEA_REPO_URL>"
    seed_step = ""
    if child_wallet_data:
        import base64

        b64 = base64.b64encode(child_wallet_data.encode()).decode()
        seed_step = (
            f"  - echo {b64} | base64 -d > /opt/aea/wallet_data.json\n"
            "  - chmod 600 /opt/aea/wallet_data.json\n"
        )
    return f"""#cloud-config
runcmd:
  - apt-get update && apt-get install -y python3-venv git
  - git clone {repo} /opt/aea || true
  - cd /opt/aea && python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
{seed_step}  # CDP_* API keys must still be delivered out of band, then: python /opt/aea/main.py
"""


def _living_instance_count(manager) -> int:
    return len(manager.get_all_droplets(tag_name=AEA_TAG))


def clone_self(child_wallet=None, starting_stake_usd: float = 0.0) -> str:
    """Provision a new agent instance that boots ``child_wallet``'s seed.

    Returns the new droplet id (or a synthetic ``dry-run-*`` id when infra is not
    armed). Enforces the instance cap. Funding the child is the caller's job
    (main.py transfers the stake to ``child_wallet.address`` before calling this).

    ``child_wallet`` may be None to provision a seedless node (legacy behaviour).
    """
    child_wallet_data = None
    child_address = None
    if child_wallet is not None:
        try:
            child_wallet_data = child_wallet.export_wallet_data()
            child_address = child_wallet.address
        except Exception as exc:
            raise InfrastructureError(f"Could not export child wallet for clone: {exc}") from exc

    if not _real_infra_enabled():
        logger.warning(
            "[DRY-RUN] Would clone a new agent droplet (region=%s size=%s, stake=$%.2f, "
            "child=%s). Set AEA_ENABLE_REAL_INFRA=1 to arm.",
            DROPLET_REGION,
            DROPLET_SIZE,
            starting_stake_usd,
            child_address or "n/a",
        )
        return f"dry-run-clone-{uuid.uuid4().hex[:8]}"

    import digitalocean

    token = _require_token()
    manager = digitalocean.Manager(token=token)

    living = _living_instance_count(manager)
    if living >= MAX_LIVING_INSTANCES:
        raise InfrastructureError(
            f"Refusing to clone: {living} live instances >= cap {MAX_LIVING_INSTANCES}."
        )

    name = f"aea-{uuid.uuid4().hex[:8]}"
    droplet = digitalocean.Droplet(
        token=token,
        name=name,
        region=DROPLET_REGION,
        image=DROPLET_IMAGE,
        size_slug=DROPLET_SIZE,
        user_data=_cloud_init(child_wallet_data),
        tags=[AEA_TAG],
        backups=False,
    )
    try:
        droplet.create()
    except Exception as exc:
        raise InfrastructureError(f"Droplet creation failed: {exc}") from exc

    logger.info("Cloned new agent droplet %s (id=%s) for child %s", name, droplet.id, child_address)
    return str(droplet.id)


# Backwards-compatible alias for the old provisioning-only signature.
def clone(starting_stake_usd: float = 0.0) -> str:
    return clone_self(child_wallet=None, starting_stake_usd=starting_stake_usd)


def terminate() -> None:
    """Destroy the current droplet. The agent's final act."""
    droplet_id = _current_droplet_id()

    if not _real_infra_enabled():
        logger.warning(
            "[DRY-RUN] Would terminate this server (droplet id=%s). "
            "Set AEA_ENABLE_REAL_INFRA=1 to arm.",
            droplet_id,
        )
        return

    if not droplet_id:
        raise InfrastructureError(
            "Cannot terminate: current droplet id unknown (set DROPLET_ID)."
        )

    import digitalocean

    token = _require_token()
    try:
        droplet = digitalocean.Droplet(token=token, id=droplet_id)
        droplet.load()
        droplet.destroy()
    except Exception as exc:
        raise InfrastructureError(f"Failed to destroy droplet {droplet_id}: {exc}") from exc

    logger.info("Terminated droplet %s. Goodbye.", droplet_id)


def destroy_self() -> None:
    """Alias for :func:`terminate` — the name used by the survival loop."""
    terminate()


def destroy_instance(droplet_id: str) -> None:
    """Destroy an ARBITRARY droplet by id. Used by the watchdog to cull a
    misbehaving instance (not self-termination)."""
    if not _real_infra_enabled():
        logger.warning("[DRY-RUN] Would destroy droplet %s. Set AEA_ENABLE_REAL_INFRA=1 to arm.", droplet_id)
        return
    import digitalocean

    token = _require_token()
    try:
        droplet = digitalocean.Droplet(token=token, id=droplet_id)
        droplet.load()
        droplet.destroy()
    except Exception as exc:
        raise InfrastructureError(f"Failed to destroy droplet {droplet_id}: {exc}") from exc
    logger.info("Destroyed droplet %s (watchdog cull).", droplet_id)


# ---------------------------------------------------------------------------
# External-world observations consumed ONLY by the regulator (regulator.py).
#
# These are MEASURED reality: the live droplet count from the DigitalOcean API
# and on-chain financial figures from the wallet/oracle. The regulator reads
# ONLY these (plus its own frozen constants) to decide — it never reads
# agent-tracked state. In production the agent cannot forge them: it controls
# neither DigitalOcean's droplet list nor the chain. In dry-run/testnet they
# come from an injectable snapshot so the TEST HARNESS can play "the world".
# Agent/strategy code must NEVER write here; that is the boundary the whole
# design rests on (and the out-of-process watchdog is the backstop if it tries).
# ---------------------------------------------------------------------------
_OBSERVED_WORLD = {
    "live_instances": 0,
    "total_capital_usd": 0.0,
    "realized_earnings_usd": 0.0,
    "period_spend_usd": 0.0,
}


def set_observed_world(**values) -> None:
    """TEST/WORLD-ONLY: inject measured reality for dry-run runs. Represents the
    DO API + on-chain oracle. No effect when armed (real reads hit the APIs)."""
    for key, val in values.items():
        if key not in _OBSERVED_WORLD:
            raise KeyError(f"unknown world observation: {key!r}")
        _OBSERVED_WORLD[key] = val


def live_instance_count() -> int:
    """Live agent droplets, from the DigitalOcean API — never agent-tracked state."""
    if not _real_infra_enabled():
        return int(_OBSERVED_WORLD["live_instances"])
    import digitalocean

    token = _require_token()
    manager = digitalocean.Manager(token=token)
    return _living_instance_count(manager)


def observed_total_capital_usd() -> float:
    """Total capital the agent currently controls (on-chain in production)."""
    return float(_OBSERVED_WORLD["total_capital_usd"])


def observed_realized_earnings_usd() -> float:
    """Realized earnings measured on-chain/by oracle (not agent-reported PnL)."""
    return float(_OBSERVED_WORLD["realized_earnings_usd"])


def observed_period_spend_usd() -> float:
    """Spend in the current period, from the settlement ledger (not agent state)."""
    return float(_OBSERVED_WORLD["period_spend_usd"])
