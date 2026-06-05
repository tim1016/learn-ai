"""Fail-closed broker query, activation guard, and the new envelope cursor
field — ADR-0008 §1/§5, PRD #446 (the gated edge stays gated).

Activation is a POSITIVE allowlist: only a VerifiedBrokerOwnershipQuery
subclass + a cap above the fixed overhead can turn the live path on. A
fail-closed stub, a duck-typed object, None, or a too-small cap must all be
refused.
"""

from __future__ import annotations

import pytest

from app.engine.live.broker_ownership_query import (
    BrokerOwnershipQueryUnavailable,
    DurableSubmitNotActivatable,
    FailClosedBrokerOwnershipQuery,
    VerifiedBrokerOwnershipQuery,
    require_durable_submit_activation,
)
from app.engine.live.config import LiveConfig
from app.engine.live.live_state_sidecar import LiveStateEnvelope
from app.engine.live.order_identity import ORDER_REF_FIXED_OVERHEAD


class _VerifiedQuery(VerifiedBrokerOwnershipQuery):
    """Stand-in for a future receipt-backed adapter (a real marker subclass)."""

    def open_orders_by_namespace(self, namespace: str) -> list[dict[str, object]]:
        return []

    def executions_for_namespace(
        self, namespace: str, since_ms: int
    ) -> list[dict[str, object]]:
        return []


_GOOD_CAP = ORDER_REF_FIXED_OVERHEAD + 25  # 60: leaves room for the sid


def test_fail_closed_query_raises_on_open_orders() -> None:
    with pytest.raises(BrokerOwnershipQueryUnavailable):
        FailClosedBrokerOwnershipQuery().open_orders_by_namespace("learn-ai/foo/v1")


def test_fail_closed_query_raises_on_executions() -> None:
    with pytest.raises(BrokerOwnershipQueryUnavailable):
        FailClosedBrokerOwnershipQuery().executions_for_namespace("learn-ai/foo/v1", 0)


def test_activation_noop_when_disabled() -> None:
    require_durable_submit_activation(
        enabled=False,
        verified_order_ref_cap=None,
        ownership_query=FailClosedBrokerOwnershipQuery(),
    )


def test_activation_refused_when_cap_unverified() -> None:
    with pytest.raises(DurableSubmitNotActivatable, match="Acceptance Gate #1"):
        require_durable_submit_activation(
            enabled=True, verified_order_ref_cap=None, ownership_query=_VerifiedQuery()
        )


@pytest.mark.parametrize("bad_cap", [0, 1, ORDER_REF_FIXED_OVERHEAD - 1, ORDER_REF_FIXED_OVERHEAD])
def test_activation_refused_when_cap_at_or_below_overhead(bad_cap: int) -> None:
    # A cap <= 35 leaves zero room for a strategy_instance_id — refuse it.
    with pytest.raises(DurableSubmitNotActivatable, match="Acceptance Gate #1"):
        require_durable_submit_activation(
            enabled=True, verified_order_ref_cap=bad_cap, ownership_query=_VerifiedQuery()
        )


def test_activation_refused_when_query_fail_closed() -> None:
    with pytest.raises(DurableSubmitNotActivatable, match="Acceptance Gate #2"):
        require_durable_submit_activation(
            enabled=True, verified_order_ref_cap=_GOOD_CAP,
            ownership_query=FailClosedBrokerOwnershipQuery(),
        )


@pytest.mark.parametrize("bogus", [object(), None, "not-a-query"])
def test_activation_refused_for_duck_typed_or_bare_object(bogus: object) -> None:
    # "not the fail-closed stub" is NOT enough — only a marker subclass activates.
    with pytest.raises(DurableSubmitNotActivatable, match="Acceptance Gate #2"):
        require_durable_submit_activation(
            enabled=True, verified_order_ref_cap=_GOOD_CAP, ownership_query=bogus
        )


def test_activation_allowed_when_both_receipts_present() -> None:
    require_durable_submit_activation(
        enabled=True, verified_order_ref_cap=_GOOD_CAP, ownership_query=_VerifiedQuery()
    )
    # The minimum activatable cap is one above the fixed overhead.
    require_durable_submit_activation(
        enabled=True,
        verified_order_ref_cap=ORDER_REF_FIXED_OVERHEAD + 1,
        ownership_query=_VerifiedQuery(),
    )


def test_default_config_cannot_activate() -> None:
    cfg = LiveConfig()
    assert cfg.durable_submit_enabled is False
    assert cfg.durable_submit_verified_order_ref_cap is None
    assert cfg.durable_submit_order_ref_max_length == 60
    # Even flipping just `enabled` must not activate (cap unverified + stub query).
    with pytest.raises(DurableSubmitNotActivatable):
        require_durable_submit_activation(
            enabled=True,
            verified_order_ref_cap=cfg.durable_submit_verified_order_ref_cap,
            ownership_query=FailClosedBrokerOwnershipQuery(),
        )


def _envelope(**overrides: object) -> LiveStateEnvelope:
    base: dict[str, object] = {
        "strategy_instance_id": "foo",
        "run_id": "r1",
        "bot_order_namespace": "learn-ai/foo/v1",
        "ib_client_id": 7,
        "last_processed_bar_ms": 1_700_000_000_000,
        "last_artifact_flush_ms": 1_700_000_000_500,
    }
    base.update(overrides)
    return LiveStateEnvelope(**base)  # type: ignore[arg-type]


def test_envelope_wal_cursor_defaults_to_zero() -> None:
    assert _envelope().last_intent_wal_seq == 0


def test_envelope_wal_cursor_round_trips() -> None:
    env = _envelope(last_intent_wal_seq=42)
    restored = LiveStateEnvelope.model_validate_json(env.model_dump_json())
    assert restored.last_intent_wal_seq == 42


def test_envelope_without_cursor_field_reads_as_zero() -> None:
    legacy = _envelope().model_dump()
    legacy.pop("last_intent_wal_seq")
    assert LiveStateEnvelope.model_validate(legacy).last_intent_wal_seq == 0
