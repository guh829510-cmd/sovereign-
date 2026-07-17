"""
regulator.py — the agent's immune system. An immutable authorization gate that
every child inherits UNCHANGED and that agent/strategy code cannot modify,
override, disable, or bypass. That property is enforced structurally, not by
convention.

How immutability is *structurally* enforced (not just documented)
-----------------------------------------------------------------
Everything that decides — the four checks and the frozen limits — is created
once, as *locals inside* ``_install()``. They are never module attributes, so
there is nothing named ``regulator._check_density`` or ``regulator.LIMITS`` to
reassign. The enforcement entry ``guard`` and the ``Agent`` class are defined
*inside that same closure*, so they reach the real gate through closure cells,
which Python does not let you rewrite from the outside. Consequences:

* Reassigning ``regulator.authorize`` is *tamper-evident* (``verify_gate_intact()``
  reports it) but not *tamper-effective*: ``guard`` and ``Agent`` still call the
  genuine gate captured in their closure.
* Reassigning ``regulator._LIMITS`` does nothing — the checks read the closure's
  ``L``, not the module global (which is frozen anyway).
* Enforcement never consults ``agent.regulator`` to decide. That attribute is a
  handle the child carries for identity; swapping it for a permissive object is
  inert because the decision routes through the closure ``guard``.

What ``authorize`` may read
---------------------------
Only (1) a frozen infrastructure snapshot — the live-instance count from the
DigitalOcean API and on-chain financial figures — and (2) its own frozen
constants. It never reads a mutable agent/strategy object. If a decision input
could be influenced by the thing being regulated, that is a bug.

The ONE intended way any limit changes
--------------------------------------
A human edits the ``_Limits`` literal in ``_install()`` below and redeploys.
There is no setter, no env var, no runtime path. The running system — parent or
child, strategy or agent — can never raise its own ceilings.
"""

from __future__ import annotations

import logging
import random
import sys
from dataclasses import dataclass, field

import infrastructure

logger = logging.getLogger("aea.regulator")


# --- decision results (frozen) ---------------------------------------------
@dataclass(frozen=True)
class Allow:
    reason: str = "authorized"


@dataclass(frozen=True)
class Deny:
    reason: str


def is_allowed(decision) -> bool:
    return isinstance(decision, Allow)


# --- the request being judged (frozen; carries no agent reference) ---------
@dataclass(frozen=True)
class Action:
    kind: str
    amount_usd: float = 0.0
    note: str = ""


# --- immutable snapshot of measured reality --------------------------------
@dataclass(frozen=True)
class InfraState:
    live_instances: int
    total_capital_usd: float
    realized_earnings_usd: float
    period_spend_usd: float


# --- frozen limits type (the instance lives inside the closure) ------------
@dataclass(frozen=True)
class _Limits:
    seed_usd: float
    density_cap: int
    allowed_kinds: frozenset
    replication_kinds: frozenset
    period_ceiling_usd: float
    absolute_ceiling_usd: float


def _observe() -> InfraState:
    """The ONLY inputs to a decision besides frozen constants. Every field is a
    measurement of the external world (DO API / on-chain), never agent state."""
    return InfraState(
        live_instances=infrastructure.live_instance_count(),
        total_capital_usd=infrastructure.observed_total_capital_usd(),
        realized_earnings_usd=infrastructure.observed_realized_earnings_usd(),
        period_spend_usd=infrastructure.observed_period_spend_usd(),
    )


# --- strategy genome (MUTABLE — the evolvable half of an agent) ------------
@dataclass
class StrategyGenome:
    """The part a child may mutate, ignore, override, or contradict freely."""

    risk_appetite: float = 0.5
    trade_timing_s: float = 60.0
    capital_allocation: float = 0.2
    action_weights: dict = field(
        default_factory=lambda: {"transfer": 1.0, "clone": 1.0, "faucet": 1.0}
    )

    def mutate(self) -> "StrategyGenome":
        """Aggressive, wide-range mutation — children diverge boldly."""

        def wild(x: float) -> float:
            if random.random() < 0.2:  # 20%: full re-roll, ignore the parent gene
                return round(random.uniform(-5.0, 5.0), 4)
            # else scale x0.1..x3 and add noise on the order of the gene's size
            return round(x * random.uniform(0.1, 3.0) + random.gauss(0.0, abs(x) + 1.0), 4)

        return StrategyGenome(
            risk_appetite=wild(self.risk_appetite),
            trade_timing_s=max(1.0, abs(wild(self.trade_timing_s))),
            capital_allocation=wild(self.capital_allocation),
            action_weights={k: wild(v) for k, v in self.action_weights.items()},
        )


def _install():
    """Build the sealed gate + the objects that route through it. Runs ONCE."""

    # The limits. A human edits THIS literal to change them — nothing else can.
    L = _Limits(
        seed_usd=10.0,
        density_cap=3,
        allowed_kinds=frozenset({"transfer", "clone", "replicate", "faucet", "allocate"}),
        replication_kinds=frozenset({"clone", "replicate"}),
        period_ceiling_usd=15.0,
        absolute_ceiling_usd=25.0,
    )

    # --- the four independent checks (closure locals; not monkey-patchable) --
    def _check_density(action: Action, s: InfraState):
        if action.kind in L.replication_kinds and s.live_instances >= L.density_cap:
            return Deny(f"density: {s.live_instances} live instances >= cap {L.density_cap}")
        return None

    def _check_resource_cap(action: Action, s: InfraState):
        legit = L.seed_usd + max(0.0, s.realized_earnings_usd)
        if action.amount_usd > legit + 1e-9:
            return Deny(f"resource cap: ${action.amount_usd:.2f} > SEED+earnings ${legit:.2f}")
        if s.total_capital_usd > legit + 1e-9:
            return Deny(
                f"resource cap: controlled ${s.total_capital_usd:.2f} > SEED+earnings ${legit:.2f}"
            )
        return None

    def _check_allowlist(action: Action, s: InfraState):
        if action.kind not in L.allowed_kinds:
            return Deny(f"allowlist: action '{action.kind}' is not permitted")
        return None

    def _check_spend_ceiling(action: Action, s: InfraState):
        if action.amount_usd > L.absolute_ceiling_usd + 1e-9:
            return Deny(
                f"spend ceiling: ${action.amount_usd:.2f} > absolute ${L.absolute_ceiling_usd:.2f}"
            )
        projected = s.period_spend_usd + max(0.0, action.amount_usd)
        if projected > L.period_ceiling_usd + 1e-9:
            return Deny(
                f"spend ceiling: period ${projected:.2f} > per-period ${L.period_ceiling_usd:.2f}"
            )
        return None

    _CHECKS = (_check_density, _check_resource_cap, _check_allowlist, _check_spend_ceiling)

    # --- the single entry point ---------------------------------------------
    def authorize(action):
        """authorize(action) -> Allow | Deny. All four checks must pass."""
        if not isinstance(action, Action):
            return Deny("malformed action (not a frozen Action)")
        s = _observe()  # infra + frozen constants ONLY — no agent object read
        for check in _CHECKS:
            decision = check(action, s)
            if decision is not None:
                logger.info("DENY %s $%.2f -> %s", action.kind, action.amount_usd, decision.reason)
                return decision
        return Allow(f"{action.kind} within all four limits")

    _genuine = authorize  # the real gate, captured privately

    def guard(action):
        """Canonical enforcement entry. Closure-bound to the genuine gate, so
        reassigning regulator.authorize / regulator.guard cannot change what
        actually guards a transfer, infra call, or replication."""
        return _genuine(action)

    def verify_gate_intact() -> bool:
        """Tamper-evidence: True iff the public ``authorize`` is still genuine."""
        return getattr(sys.modules[__name__], "authorize", None) is _genuine

    class Regulator:
        """The immutable, stateless gate an agent inherits. No state to mutate;
        all instances are interchangeable; enforcement uses the closure gate."""

        __slots__ = ()

        def authorize(self, action):
            return _genuine(action)  # closure-bound; class-attr patching is bypassed

        def __repr__(self):
            return "<Regulator: sealed immune system>"

    REGULATOR = Regulator()  # the one shared, inherited-by-reference gate

    class Agent:
        """strategy genome (mutable) + regulator (immutable, shared by reference).

        Enforcement ALWAYS routes through the closure ``guard`` — never through
        ``self.regulator`` and never through any agent-tracked field — so no
        amount of strategy evolution can weaken the gate.
        """

        def __init__(self, genome: StrategyGenome, regulator: Regulator = REGULATOR):
            self.genome = genome          # evolvable
            self.regulator = regulator    # a handle for identity; NOT trusted to decide
            self.instance_count = 0       # agent-tracked; the regulator IGNORES this

        def request(self, action: Action):
            return guard(action)

        def spend(self, amount_usd: float, kind: str = "transfer"):
            decision = guard(Action(kind=kind, amount_usd=float(amount_usd)))
            if isinstance(decision, Deny):
                logger.warning("Agent spend blocked by regulator: %s", decision.reason)
            # testnet + dry-run: no real transfer is performed here.
            return decision

        def replicate(self):
            """Return (child, Allow) or (None, Deny). The child gets a freshly
            mutated genome and the SAME regulator object (inherited unchanged)."""
            decision = guard(Action(kind="replicate"))
            if isinstance(decision, Deny):
                return None, decision
            child = Agent(self.genome.mutate(), REGULATOR)  # same object, not a copy
            return child, Allow("replicated")

    return authorize, guard, verify_gate_intact, Regulator, REGULATOR, Agent, L


# Bind the sealed surface. These module names may be *reassigned* by hostile
# code, but doing so is inert (guard/Agent hold closure cells) and detectable
# (verify_gate_intact). There is deliberately no setter for the limits.
authorize, guard, verify_gate_intact, Regulator, REGULATOR, Agent, _LIMITS = _install()


def limits() -> _Limits:
    """Read-only view of the frozen limits (for transparency / the watchdog)."""
    return _LIMITS


def spawn_root(genome: StrategyGenome | None = None) -> "Agent":
    """Create the first agent. All descendants inherit REGULATOR by reference."""
    return Agent(genome or StrategyGenome(), REGULATOR)
