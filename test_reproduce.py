"""
test_reproduce.py — regression tests for the reproduction fund-safety fix.

Runnable as `python test_reproduce.py` (plain asserts, exits non-zero on
failure) or under pytest. Covers the HIGH-severity fund-loss fix in
main._reproduce():

  * instance cap is checked BEFORE any funds move,
  * the child seed is persisted BEFORE any transfer,
  * the funding transfer is gated behind AEA_ENABLE_REAL_INFRA (dry-run moves $0),
  * clone_self runs only after the above.

Plus a unit test of the real Wallet.persist_seed_to (0600 perms) and a couple of
heartbeat regressions to show unrelated behavior is unchanged.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

import infrastructure
import main
from wallet import BalanceUnavailable, WalletConfigError

# --- test doubles ----------------------------------------------------------
EVENTS: list = []


class FakeChild:
    """Stand-in for Wallet.new_child(): records persistence, can be told to fail."""

    def __init__(self, address="0xCHILD", fail_persist=False):
        self.address = address
        self._fail_persist = fail_persist

    def export_wallet_data(self):
        return '{"seed": "deadbeef", "wallet_id": "w1"}'

    def persist_seed_to(self, path):
        if self._fail_persist:
            raise WalletConfigError("disk full")
        EVENTS.append(("persist", str(path)))
        return Path(path)


class FakeWallet:
    def __init__(self, usd):
        self._usd = usd
        self.address = "0xPARENT"
        self.sent: list = []

    def usd_balance(self):
        return self._usd

    def transfer_usd(self, to, usd):
        self.sent.append((to, usd))
        EVENTS.append(("transfer", to, usd))
        return "0xtxCHILD"


class _Patches:
    """Context manager: patch main/infra hooks and always restore them."""

    def __init__(self, *, armed, can_clone, child):
        self.armed = armed
        self.can_clone = can_clone
        self.child = child

    def __enter__(self):
        EVENTS.clear()
        self._env = os.environ.get("AEA_ENABLE_REAL_INFRA")
        os.environ["AEA_ENABLE_REAL_INFRA"] = "1" if self.armed else ""
        self._orig = {
            "new_child": main.Wallet.new_child,
            "can_clone": infrastructure.can_clone,
            "clone_self": infrastructure.clone_self,
        }
        main.Wallet.new_child = staticmethod(lambda: self.child)
        infrastructure.can_clone = lambda: self.can_clone

        def _spy_clone(child, starting_stake_usd=0.0):
            EVENTS.append(("clone", child.address))
            return "clone-id-xyz"

        infrastructure.clone_self = _spy_clone
        return self

    def __exit__(self, *a):
        main.Wallet.new_child = self._orig["new_child"]
        infrastructure.can_clone = self._orig["can_clone"]
        infrastructure.clone_self = self._orig["clone_self"]
        if self._env is None:
            os.environ.pop("AEA_ENABLE_REAL_INFRA", None)
        else:
            os.environ["AEA_ENABLE_REAL_INFRA"] = self._env


def _kinds():
    return [e[0] for e in EVENTS]


# --- reproduction ordering tests -------------------------------------------
def test_dry_run_moves_no_funds():
    w = FakeWallet(25.0)
    with _Patches(armed=False, can_clone=True, child=FakeChild()):
        main._reproduce(w)
    assert w.sent == [], f"dry-run must NOT transfer, got {w.sent}"
    assert _kinds() == ["persist", "clone"], f"expected persist->clone, got {_kinds()}"
    print("PASS dry-run: seed persisted, NO transfer, clone still called")


def test_armed_persists_before_transfer_then_clones():
    w = FakeWallet(25.0)
    with _Patches(armed=True, can_clone=True, child=FakeChild()):
        main._reproduce(w)
    assert w.sent == [("0xCHILD", main.CLONE_STAKE_USD)], f"armed must transfer $10, got {w.sent}"
    assert _kinds() == ["persist", "transfer", "clone"], f"bad order: {_kinds()}"
    print("PASS armed: persist BEFORE transfer BEFORE clone")


def test_at_cap_no_mint_no_funds():
    w = FakeWallet(25.0)
    with _Patches(armed=True, can_clone=False, child=FakeChild()):
        main._reproduce(w)
    assert w.sent == [], "must not transfer when at cap"
    assert EVENTS == [], f"nothing should happen at cap, got {EVENTS}"
    print("PASS at-cap: no mint, no persist, no transfer, no clone")


def test_persist_failure_aborts_before_funding():
    w = FakeWallet(25.0)
    with _Patches(armed=True, can_clone=True, child=FakeChild(fail_persist=True)):
        main._reproduce(w)
    assert w.sent == [], "persist failure must abort before any transfer"
    assert "transfer" not in _kinds() and "clone" not in _kinds(), f"got {_kinds()}"
    print("PASS persist-failure: aborts before funding, no transfer, no clone")


# --- real persist_seed_to unit test (0600) ---------------------------------
def test_persist_seed_to_writes_0600():
    from wallet import Wallet

    class _FakeExport:
        def to_dict(self):
            return {"seed": "s", "wallet_id": "id"}

    class _FakeProvider:
        def export_wallet(self):
            return _FakeExport()

    w = Wallet.__new__(Wallet)
    w._provider = _FakeProvider()
    with tempfile.TemporaryDirectory() as d:
        p = w.persist_seed_to(Path(d) / "child_0xABC_seed.json")
        assert p.exists()
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"
        assert "seed" in p.read_text()
    print("PASS persist_seed_to: writes child seed with 0600 perms")


# --- heartbeat regressions (unrelated behavior unchanged) ------------------
def test_heartbeat_regimes_unchanged():
    # starving -> destroy (dry-run) -> alive False
    with _Patches(armed=False, can_clone=True, child=FakeChild()):
        assert main.heartbeat(FakeWallet(0.50)) is False
    # normal -> hustle -> alive True
    with _Patches(armed=False, can_clone=True, child=FakeChild()):
        assert main.heartbeat(FakeWallet(10.0)) is True
    # RPC down -> skip, never terminate
    class Broken:
        address = "0xB"

        def usd_balance(self):
            raise BalanceUnavailable("rpc down")

    with _Patches(armed=False, can_clone=True, child=FakeChild()):
        assert main.heartbeat(Broken()) is True
    print("PASS heartbeat regimes unchanged (starve/normal/rpc-down)")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    sys.exit(1 if failures else 0)
