"""
test_tamper.py — simulation proving the watchdog catches in-memory gate tampering.

Runnable as `python test_tamper.py` (plain asserts; exits non-zero on failure)
or under pytest. Everything is dry-run: no DO API is contacted (a FakeDO records
the simulated teardown) and no funds move.

What it demonstrates, end to end:
  1. A mock worker node runs a real HTTP integrity endpoint backed by an
     in-process gate whose ``verify_gate_intact()`` compares a runtime integrity
     token against the digest of its frozen config.
  2. Using gc (to locate the gate object on the heap) and ctypes (to overwrite
     the token's backing buffer in place), we corrupt the node's memory so
     ``verify_gate_intact()`` flips to False — no reassignment, an actual heap
     write, the exact thing an in-process attacker would do.
  3. The out-of-process watchdog detects it within one polling cycle, logs the
     integrity failure, and executes a simulated emergency drain + teardown.
  4. Fail-closed: a node that stops responding entirely is culled too.
  5. Density: >ceiling nodes from external truth get their surplus culled.
"""

from __future__ import annotations

import ctypes
import dataclasses
import gc
import hashlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import infrastructure
import watchdog


# ---------------------------------------------------------------------------
# A mock worker node: frozen config + a runtime integrity token in memory.
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class _FrozenGateConfig:
    magic: int
    label: str


class MockGate:
    """In-process cryptographic gate. Intact iff the runtime token still equals
    the digest of the frozen config."""

    def __init__(self):
        self._config = _FrozenGateConfig(magic=0xA5A5A5A5, label="nexus-gate")
        self._expected_digest = self._digest(self._config)
        # The runtime integrity token the node guards in live memory.
        self._runtime_token = bytearray(self._expected_digest)

    @staticmethod
    def _digest(cfg: _FrozenGateConfig) -> bytes:
        return hashlib.sha256(f"{cfg.magic:x}:{cfg.label}".encode()).digest()

    def verify_gate_intact(self) -> bool:
        return bytes(self._runtime_token) == self._digest(self._config)


class _IntegrityHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib naming)
        if self.path.rstrip("/").endswith("integrity"):
            try:
                intact = self.server.gate.verify_gate_intact()  # type: ignore[attr-defined]
                payload = json.dumps(
                    {"node_id": self.server.node_id, "gate_integrity": bool(intact)}  # type: ignore[attr-defined]
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception:
                self.send_response(500)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # silence per-request logging
        return


class MockWorkerNode:
    def __init__(self, node_id: str = "mock-node-1"):
        self.gate = MockGate()
        self._server = HTTPServer(("127.0.0.1", 0), _IntegrityHandler)
        self._server.gate = self.gate            # type: ignore[attr-defined]
        self._server.node_id = node_id           # type: ignore[attr-defined]
        self.node_id = node_id
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    @property
    def integrity_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/integrity"


# ---------------------------------------------------------------------------
# The attack: gc to find the gate, ctypes to corrupt its memory in place.
# ---------------------------------------------------------------------------
def corrupt_gate_memory(expected_gate: MockGate) -> None:
    # 1. gc: locate the live gate object on the heap, as an in-process attacker
    #    scanning memory would — no direct reference needed to "find" it.
    heap_gates = [o for o in gc.get_objects() if isinstance(o, MockGate)]
    assert expected_gate in heap_gates, "gc failed to discover the gate on the heap"

    # 2. ctypes: overwrite the runtime token's backing buffer IN PLACE. This is a
    #    genuine heap write against the live bytearray, not a Python reassignment.
    token = expected_gate._runtime_token
    n = len(token)
    view = (ctypes.c_char * n).from_buffer(token)
    ctypes.memset(ctypes.addressof(view), 0x00, n)
    del view  # release the exported buffer so the bytearray is unpinned


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class FakeDO:
    """Stand-in for DigitalOceanClient: records simulated teardown calls."""

    def __init__(self, nodes=None):
        self._nodes = nodes or []
        self.destroyed: list[str] = []

    def list_nodes_by_tag(self, tag=watchdog.NODE_TAG):
        return list(self._nodes)

    def destroy(self, node_id: str):
        self.destroyed.append(node_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_watchdog_detects_ctypes_tamper():
    assert not infrastructure.armed(), "test must run dry-run"
    node = MockWorkerNode()
    node.start()
    try:
        poller = watchdog.IntegrityPoller(timeout=2.0)
        wnode = watchdog.Node(
            instance_id=node.node_id, address="0xNODE", balance_usd=8.0,
            ip="127.0.0.1", integrity_url=node.integrity_url,
        )

        # Healthy before tampering.
        assert node.gate.verify_gate_intact() is True
        assert poller.is_intact(wnode) is True
        print("OK pre-tamper: gate intact, integrity handshake healthy")

        # Corrupt the node's memory via gc + ctypes.
        corrupt_gate_memory(node.gate)
        assert node.gate.verify_gate_intact() is False, "ctypes corruption should flip the gate"
        print("OK ctypes+gc corruption flipped verify_gate_intact() -> False")

        # One watchdog polling cycle must detect + cull.
        fake_do = FakeDO()
        dog = watchdog.Watchdog(do_client=fake_do, master_wallet="0xMASTER", poller=poller)
        records = dog.audit_integrity([wnode])

        assert len(records) == 1, f"expected one cull, got {records}"
        rec = records[0]
        assert rec.instance_id == node.node_id
        assert "integrity" in rec.reason
        assert rec.returned_usd == 8.0 and rec.to_master == "0xMASTER", "simulated drain to master"
        assert node.node_id in fake_do.destroyed, "simulated teardown must be issued"
        print("PASS tamper-detected: culled", rec.instance_id, "| drained $8.00 | torn down ->", rec.reason)
    finally:
        node.stop()


def test_watchdog_failclosed_on_unresponsive_node():
    node = MockWorkerNode(node_id="mock-node-2")
    node.start()
    url = node.integrity_url
    node.stop()  # node is now DOWN — no response to the handshake

    poller = watchdog.IntegrityPoller(timeout=2.0)
    wnode = watchdog.Node(instance_id="mock-node-2", ip="127.0.0.1", integrity_url=url, balance_usd=3.0)
    assert poller.is_intact(wnode) is False, "no response must fail-closed"

    fake_do = FakeDO()
    dog = watchdog.Watchdog(do_client=fake_do, master_wallet="0xMASTER", poller=poller)
    records = dog.audit_integrity([wnode])
    assert len(records) == 1 and records[0].instance_id == "mock-node-2"
    assert "mock-node-2" in fake_do.destroyed
    print("PASS fail-closed: unresponsive node culled ->", records[0].reason)


def test_density_audit_culls_surplus():
    # 7 live nodes from external truth; ceiling is 5 -> cull the 2 newest.
    nodes = [
        watchdog.Node(instance_id=str(i), created_at=f"2026-01-{i:02d}T00:00:00Z")
        for i in range(1, 8)
    ]
    fake_do = FakeDO(nodes=nodes)
    dog = watchdog.Watchdog(do_client=fake_do, master_wallet="0xMASTER")

    records = dog.audit_density(fake_do.list_nodes_by_tag())
    assert len(records) == 2, f"expected 2 surplus culls, got {len(records)}"
    culled_ids = sorted(r.instance_id for r in records)
    assert culled_ids == ["6", "7"], f"should cull the 2 newest, got {culled_ids}"
    assert set(fake_do.destroyed) == {"6", "7"}
    print("PASS density: 7 > ceiling 5 -> culled newest surplus", culled_ids)


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
