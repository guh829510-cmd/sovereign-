"""
infrastructure.py — the agent's body: birth and death of servers.

Uses python-digitalocean to control the compute the agent runs on. Two
operations matter:

    terminate()  -> destroy this droplet when the agent can no longer pay
                    its bills (balance < $1). This is the agent "dying".
    clone()      -> provision a NEW droplet, deploy this same codebase, and
                    hand it a starting stake, when the agent is wealthy
                    (balance > $20). This is the agent "reproducing".

Safety notes for Step 3:
    * clone() MUST enforce a hard cap on total living instances and a spend
      cap on the DigitalOcean account, so a bug can't fork-bomb real servers
      and run up a real bill. Self-replication without a governor is how you
      get a surprise invoice.
    * terminate() should flush state (wallet seed, ledger) somewhere durable
      first, and be the very last thing the process does.
"""

from __future__ import annotations

# Guard rails — real money / real servers live on the other side of these.
MAX_LIVING_INSTANCES = 3
DROPLET_REGION = "nyc1"
DROPLET_SIZE = "s-1vcpu-1gb"


def terminate() -> None:
    """Destroy the current droplet. Implemented in Step 3."""
    raise NotImplementedError


def clone(starting_stake_usd: float) -> str:
    """Provision a new agent instance; return its droplet id. Step 3."""
    raise NotImplementedError
