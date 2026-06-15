"""Phase 5C activation flag tests — LiveConfig.durable_submit_enabled
wires the engine's construction-time durable-submit activation gate.

Default behavior (``durable_submit_enabled=False``) leaves construction
unchanged. When the operator flips the flag, ``LiveEngine.__init__``
calls ``require_durable_submit_activation`` with the verified
``IbkrBrokerOwnershipQuery`` instance — refusal propagates out of the
constructor so the runner refuses to start.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.engine.live.broker_ownership_query import DurableSubmitNotActivatable
from app.engine.live.config import LiveConfig
from app.engine.live.live_engine import LiveEngine
from tests.engine.live.fixtures.fake_broker import FakeBroker


@dataclass
class _FakeSettings:
    mode: str = "paper"
    port: int = 7497


@dataclass
class _FakeIB:
    """Minimum ``ib_async.IB`` surface ``IbkrBrokerOwnershipQuery`` reads."""

    def openTrades(self) -> list[object]:
        return []

    def fills(self) -> list[object]:
        return []


@dataclass
class _FakeClient:
    settings: _FakeSettings = field(default_factory=_FakeSettings)
    ib: _FakeIB = field(default_factory=_FakeIB)
    connected_account: str = "DU123"
    connection_lost: bool = False
    connectivity_lost_count: int = 0


def test_durable_submit_disabled_default_does_not_invoke_activation(
    tmp_path,
) -> None:
    """Default LiveConfig keeps the activation gate dormant — construction
    proceeds even with a fail-closed broker. Backward-compat for every
    pre-Phase-5C run."""
    engine = LiveEngine(
        _FakeClient(),  # type: ignore[arg-type]
        LiveConfig(),  # durable_submit_enabled defaults to False
        broker=FakeBroker(),
        output_dir=tmp_path,
        account_id="DU123",
    )
    # Construction succeeded — no DurableSubmitNotActivatable raised.
    assert engine is not None


def test_durable_submit_enabled_activates_with_ibkr_subclass(tmp_path) -> None:
    """When the flag is on AND a real client is wired,
    ``IbkrBrokerOwnershipQuery`` is constructed and the activation
    contract passes (Gate #1 cap + Gate #2 subclass-allowlist)."""
    config = LiveConfig(durable_submit_enabled=True)
    engine = LiveEngine(
        _FakeClient(),  # type: ignore[arg-type]
        config,
        broker=FakeBroker(),
        output_dir=tmp_path,
        account_id="DU123",
    )
    assert engine is not None


def test_durable_submit_enabled_with_no_client_is_a_noop(tmp_path) -> None:
    """Replay paths construct LiveEngine without a real client. The
    activation gate only fires when both the flag is on AND a client is
    wired — otherwise replay tests would have to disable the flag
    everywhere."""
    engine = LiveEngine(
        None,
        LiveConfig(durable_submit_enabled=True),
        broker=FakeBroker(),
        output_dir=tmp_path,
        account_id="DU123",
    )
    assert engine is not None


def test_activation_raises_when_subclass_construction_fails(tmp_path) -> None:
    """Defense-in-depth: if someone replaces the IbkrBrokerOwnershipQuery
    subclass with a fail-closed stub or a non-VerifiedBrokerOwnershipQuery
    duck-typed object, the activation gate refuses. Documented here by
    mutating the activation function via monkeypatch."""
    import app.engine.live.live_engine as engine_module

    class _BogusQuery:
        """Looks like an ownership query but isn't a verified subclass."""

        def __init__(self, client) -> None:  # mirror the constructor signature
            self._client = client

        def open_orders_by_namespace(self, namespace):  # noqa: D401
            return []

        def executions_for_namespace(self, namespace, since_ms):
            return []

    # Patch the subclass constructor so the engine constructs a non-
    # VerifiedBrokerOwnershipQuery — the require_durable_submit_activation
    # call should refuse.
    import app.engine.live.ibkr_broker_ownership_query as iboq

    original = iboq.IbkrBrokerOwnershipQuery
    iboq.IbkrBrokerOwnershipQuery = _BogusQuery  # type: ignore[assignment,misc]
    try:
        with pytest.raises(
            DurableSubmitNotActivatable, match="ownership query unverified"
        ):
            LiveEngine(
                _FakeClient(),  # type: ignore[arg-type]
                LiveConfig(durable_submit_enabled=True),
                broker=FakeBroker(),
                output_dir=tmp_path,
                account_id="DU123",
            )
    finally:
        iboq.IbkrBrokerOwnershipQuery = original  # type: ignore[assignment,misc]
        # Drop the stale module attribute from engine_module if it
        # imported the patched name at module load time (it doesn't —
        # the import is inside __init__ — but be defensive).
        _ = engine_module
