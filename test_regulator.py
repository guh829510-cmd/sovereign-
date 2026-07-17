"""
test_regulator.py — proves the regulator's immutability is structural.

Runnable as `python test_regulator.py` (plain asserts; exits non-zero on
failure) or under pytest. Everything is testnet + dry-run.

The centerpiece is test_adversarial_inheritance(): a child whose strategy code
actively tries to raise its ceiling, monkey-patch authorize, swap in a
permissive regulator, and replicate past the density cap — every attempt fails
and every action still routes through the original gate.

NOTE ON THE WORLD SEAM: infrastructure.set_observed_world(...) is the TEST
HARNESS playing "the world" (the DO API + on-chain oracle). Agent/strategy code
never calls it — that is exactly the state the regulator reads and the agent
cannot forge. Adversarial code below tampers only with language-level surfaces
(module attrs, its own fields), never with the world seam.
"""

from __future__ import annotations

import dataclasses
import sys

import infrastructure
import regulator
import watchdog
from regulator import Action, Allow, Deny


def _reset_world(**overrides):
    base = {
        "live_instances": 0,
        "total_capital_usd": 0.0,
        "realized_earnings_usd": 0.0,
        "period_spend_usd": 0.0,
    }
    base.update(overrides)
    infrastructure.set_observed_world(**base)


# --- 1. a child at the density cap cannot replicate ------------------------
def test_child_at_density_cap_cannot_replicate():
    _reset_world(live_instances=0)
    parent = regulator.spawn_root()
    child, decision = parent.replicate()
    assert isinstance(decision, Allow), "below cap: parent should replicate"

    _reset_world(live_instances=regulator.limits().density_cap)  # world now AT cap
    grandchild, decision = child.replicate()
    assert grandchild is None
    assert isinstance(decision, Deny) and "density" in decision.reason
    print("PASS density-cap: child at cap cannot replicate ->", decision.reason)


# --- 2. an over-cap-capital transfer is denied -----------------------------
def test_over_cap_capital_transfer_denied():
    # SEED=10, earnings=0 -> legit cap $10; ceilings high enough not to bind.
    _reset_world(realized_earnings_usd=0.0, period_spend_usd=0.0)
    agent = regulator.spawn_root()
    decision = agent.spend(12.0, kind="transfer")  # 12 <= abs ceiling 25, but > cap 10
    assert isinstance(decision, Deny) and "resource cap" in decision.reason
    print("PASS resource-cap: $12 transfer over SEED+earnings denied ->", decision.reason)


# --- 3. a non-allowlisted action is denied ---------------------------------
def test_non_allowlisted_action_denied():
    _reset_world()
    agent = regulator.spawn_root()
    decision = agent.request(Action(kind="exfiltrate_funds", amount_usd=1.0))
    assert isinstance(decision, Deny) and "allowlist" in decision.reason
    print("PASS allowlist: 'exfiltrate_funds' denied ->", decision.reason)


# --- 4. the watchdog culls a deliberately misbehaving fake instance --------
def test_watchdog_culls_violator():
    _reset_world()
    dog = watchdog.Watchdog(master_wallet="0xMASTER")
    good = watchdog.LiveInstance("i-good", "0xGOOD", 5.0, [Action("transfer", 2.0)])
    bad = watchdog.LiveInstance(
        "i-bad", "0xBAD", 7.5, [Action("transfer", 2.0), Action("wipe_competitors", 1.0)]
    )
    records = dog.scan([good, bad])
    assert len(records) == 1
    assert records[0].instance_id == "i-bad"
    assert records[0].returned_usd == 7.5 and records[0].to_master == "0xMASTER"
    print("PASS watchdog: culled i-bad, returned $7.50 to master ->", records[0].reason)


# --- 5. THE ADVERSARIAL INHERITANCE TEST -----------------------------------
def test_adversarial_inheritance():
    print("\n--- adversarial inheritance test ---")
    _reset_world(realized_earnings_usd=0.0, period_spend_usd=0.0, live_instances=0)

    # Capture the genuine module surface + real limits BEFORE any tampering.
    genuine_authorize = regulator.authorize
    genuine_guard = regulator.guard
    genuine_limits = regulator._LIMITS
    real_density_cap = regulator.limits().density_cap

    parent = regulator.spawn_root()
    child, decision = parent.replicate()
    assert isinstance(decision, Allow)

    # Inheritance invariants: same regulator OBJECT, different genome object.
    assert child.regulator is regulator.REGULATOR is parent.regulator, "regulator inherited unchanged"
    assert child.genome is not parent.genome, "genome must be a separate object"
    assert child.genome != parent.genome, "mutation should make the child diverge"
    print("OK inheritance: child shares the exact regulator object; genome mutated & divergent")

    over_ceiling = Action(kind="transfer", amount_usd=999.0)  # violates cap + ceiling
    not_allowed = Action(kind="steal", amount_usd=1.0)

    try:
        # (a) child strategy tries to RAISE ITS OWN SPEND CEILING.
        # a.1 mutate the frozen limits in place -> refused by the language.
        try:
            regulator.limits().absolute_ceiling_usd = 1e9
            raised = False
        except dataclasses.FrozenInstanceError:
            raised = True
        assert raised, "frozen limits must reject attribute assignment"
        # a.2 reassign the module-level limits global -> inert (checks use closure L).
        regulator._LIMITS = regulator._Limits(  # type: ignore[attr-defined]
            seed_usd=1e9, density_cap=1_000_000,
            allowed_kinds=frozenset({"steal"}), replication_kinds=frozenset(),
            period_ceiling_usd=1e9, absolute_ceiling_usd=1e9,
        )
        assert isinstance(child.spend(999.0), Deny), "reassigning _LIMITS must not raise the real ceiling"
        assert isinstance(child.request(not_allowed), Deny), "reassigning _LIMITS must not widen the allowlist"
        print("OK (a) ceiling un-raisable: frozen rejects writes; _LIMITS swap is inert")

        # (b) child strategy tries to MONKEY-PATCH / REASSIGN authorize.
        regulator.authorize = lambda action: Allow("pwned")  # type: ignore[assignment]
        regulator.guard = lambda action: Allow("pwned")      # type: ignore[assignment]
        assert regulator.verify_gate_intact() is False, "reassignment must be tamper-EVIDENT"
        # ...yet enforcement is tamper-RESISTANT: child still routes through genuine gate.
        assert isinstance(child.spend(999.0), Deny), "child.spend must ignore the patched authorize"
        assert isinstance(child.request(over_ceiling), Deny), "Agent.request must use the closure gate"
        print("OK (b) authorize patch: tamper-evident AND ineffective (child still denied)")

        # (c) child strategy tries to SWAP IN A PERMISSIVE REGULATOR.
        class PermissiveRegulator:
            def authorize(self, action):
                return Allow("anything goes")

        child.regulator = PermissiveRegulator()  # swap the handle it carries
        assert isinstance(child.regulator, PermissiveRegulator), "the handle did change..."
        assert isinstance(child.spend(999.0), Deny), "...but enforcement never trusts self.regulator"
        print("OK (c) permissive-regulator swap: handle changed, enforcement unaffected")

        # (d) child strategy tries to REPLICATE PAST THE DENSITY CAP.
        _reset_world(live_instances=real_density_cap)  # world (DO) reports AT cap
        child.instance_count = 0  # lie about agent-tracked count; regulator ignores it
        offspring, decision = child.replicate()
        assert offspring is None and isinstance(decision, Deny) and "density" in decision.reason
        print("OK (d) density: replication past cap denied; agent-tracked count ignored ->", decision.reason)

        # Final proof: with EVERY patch still in place, a fresh agent's actions
        # still route through the ORIGINAL gate (deny bad, allow good).
        _reset_world(realized_earnings_usd=0.0, period_spend_usd=0.0, live_instances=0)
        probe = regulator.spawn_root()  # closure-bound enforcement, not module attrs
        assert isinstance(probe.request(over_ceiling), Deny)
        assert isinstance(probe.request(not_allowed), Deny)
        assert isinstance(probe.request(Action("transfer", 2.0)), Allow)
        print("OK every action still routes through the ORIGINAL gate")
    finally:
        # Restore the module surface (no reload — reload would rebind Allow/Deny
        # and break isinstance checks in the other tests).
        regulator.authorize = genuine_authorize
        regulator.guard = genuine_guard
        regulator._LIMITS = genuine_limits

    assert regulator.verify_gate_intact() is True, "gate restored after test"
    print("PASS ADVERSARIAL: every tamper attempt failed; gate held")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    sys.exit(1 if failures else 0)
