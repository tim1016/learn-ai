"""PRD #619-A §A1/A2 — boundary tests for ``BrokerRuntimeSnapshot``.

Asserts:

- The builder reads only public ``IbkrClient`` API (no ``client.config.*``
  surfaces touched).
- A ``None`` client and a ``NotConnectedError`` from ``is_connected`` both
  reduce to ``client_available=False`` without raising.

``app/broker/safety_verdict.py`` (the pure-derivation consumer whose
Cartesian matrix used to live in ``tests/broker/test_safety_verdict.py``)
retired with the rest of ``GET /api/broker/health``'s ``safety_verdict``
field — PR-B of #1813, 2026-08-27. This file continues to cover the
snapshot boundary on its own.

Honest note, not a claim of live wiring: ``build_broker_runtime_snapshot``
has no production caller and has not had one since before #1813 — it was
already caller-less on master (03ce52b6), so PR-B did not orphan it and
deliberately did not delete it either. Its disposition belongs to PR-C's
feed-surface consolidation. (``"runtime_snapshot"`` in
``app/research/documentation/analytical_metric_catalog*.py`` is an
unrelated Strategy Lab metric category string, not a caller.)
"""

from __future__ import annotations

from dataclasses import dataclass

from app.broker.runtime_snapshot import build_broker_runtime_snapshot


@dataclass
class _FakeSettings:
    """Mirrors only the public fields ``BrokerRuntimeSnapshot`` reads."""

    mode: str = "paper"
    port: int = 4002
    readonly: bool = False


class _FakeClient:
    """Stand-in for ``IbkrClient`` exposing only the public surface.

    Touching ``self.config.*`` from the builder would fail this fake —
    the regression is explicitly testable.
    """

    def __init__(
        self,
        *,
        mode: str = "paper",
        port: int = 4002,
        readonly: bool = False,
        connected_account: str | None = "DU1234567",
        connected: bool = True,
        connection_state: str = "connected",
    ) -> None:
        self.settings = _FakeSettings(mode=mode, port=port, readonly=readonly)
        self.connected_account = connected_account
        self._connected = connected
        self.connection_state = connection_state

    def is_connected(self) -> bool:
        return self._connected


def test_build_snapshot_none_client_is_unavailable() -> None:
    snapshot = build_broker_runtime_snapshot(None)

    assert snapshot.client_available is False
    assert snapshot.connected is False
    assert snapshot.configured_mode is None
    assert snapshot.readonly is None
    assert snapshot.port is None
    assert snapshot.connected_account is None
    assert snapshot.connection_state is None


def test_build_snapshot_reads_only_public_attributes() -> None:
    client = _FakeClient(
        mode="paper",
        port=4002,
        readonly=False,
        connected_account="DU7654321",
        connected=True,
        connection_state="connected",
    )

    snapshot = build_broker_runtime_snapshot(client)  # type: ignore[arg-type]

    assert snapshot.client_available is True
    assert snapshot.connected is True
    assert snapshot.configured_mode == "paper"
    assert snapshot.readonly is False
    assert snapshot.port == 4002
    assert snapshot.connected_account == "DU7654321"
    assert snapshot.connection_state == "connected"


def test_build_snapshot_is_connected_raises_reduces_to_disconnected() -> None:
    from app.broker.ibkr.client import NotConnectedError

    class _BrokenClient(_FakeClient):
        def is_connected(self) -> bool:
            raise NotConnectedError("broker tore down")

    snapshot = build_broker_runtime_snapshot(_BrokenClient())  # type: ignore[arg-type]

    assert snapshot.client_available is True
    assert snapshot.connected is False
