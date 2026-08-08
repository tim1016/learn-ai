"""Tests for the dev-only fault-injection seam registry + crafting (PRD #1354).

Covers the seam's core guarantees: inert-by-default, posture fail-closed, and
per-fault crafting correctness. The write/frame *hooks* that thread these into
the real adapter + consumer are tested in test_client.py / test_trade_updates.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.broker.alpaca import fault_injection as fi
from app.broker.contract.errors import (
    BrokerOrderRejected,
    BrokerRateLimited,
    BrokerRequestInvalid,
    BrokerUnavailable,
)


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    fi.reset_fault_injection_for_testing()
    yield
    fi.reset_fault_injection_for_testing()


@pytest.fixture
def permit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the seam permitted: flag on + paper posture."""
    monkeypatch.setattr(fi.settings, "ALPACA_FAULT_INJECTION_ENABLED", True)
    monkeypatch.setattr(fi, "get_alpaca_settings", lambda: SimpleNamespace(is_paper=True))


# ── Gating ────────────────────────────────────────────────────────────────────


def test_seam_is_inert_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fi.settings, "ALPACA_FAULT_INJECTION_ENABLED", False)
    registry = fi.get_fault_injection_registry()

    assert fi.injection_permitted() is False
    with pytest.raises(fi.FaultInjectionRefused):
        registry.arm(fi.WriteFaultKind.REJECT_422)
    assert registry.consume_write_fault("anything") is None
    assert registry.drain_frame_faults() == []


def test_flag_on_but_not_paper_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fi.settings, "ALPACA_FAULT_INJECTION_ENABLED", True)
    monkeypatch.setattr(fi, "get_alpaca_settings", lambda: SimpleNamespace(is_paper=False))

    assert fi.injection_permitted() is False
    with pytest.raises(fi.FaultInjectionRefused):
        fi.get_fault_injection_registry().arm(fi.WriteFaultKind.CONFLICT_409)


def test_flag_on_but_posture_unresolvable_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fi.settings, "ALPACA_FAULT_INJECTION_ENABLED", True)

    def _raise() -> None:
        raise RuntimeError("no credentials")

    monkeypatch.setattr(fi, "get_alpaca_settings", _raise)

    assert fi.injection_permitted() is False


def test_permitted_requires_flag_and_paper(permit: None) -> None:
    assert fi.injection_permitted() is True


# ── Arming / bounded budget / disarm ──────────────────────────────────────────


def test_arm_rejects_unknown_kind(permit: None) -> None:
    with pytest.raises(ValueError, match="unknown fault kind"):
        fi.get_fault_injection_registry().arm("not_a_kind")


def test_arm_rejects_non_positive_count(permit: None) -> None:
    with pytest.raises(ValueError, match="count must be"):
        fi.get_fault_injection_registry().arm(fi.WriteFaultKind.TIMEOUT, count=0)


def test_write_fault_is_bounded_by_count(permit: None) -> None:
    registry = fi.get_fault_injection_registry()
    registry.arm(fi.WriteFaultKind.REJECT_422, count=2)

    assert registry.consume_write_fault("id-1") is not None
    assert registry.consume_write_fault("id-2") is not None
    # Budget exhausted.
    assert registry.consume_write_fault("id-3") is None


def test_disarm_all_clears_everything(permit: None) -> None:
    registry = fi.get_fault_injection_registry()
    registry.arm(fi.WriteFaultKind.REJECT_422)
    registry.arm(fi.FrameFaultKind.PARTIAL_FILL)

    assert registry.disarm_all() == 2
    assert registry.consume_write_fault("x") is None
    assert registry.drain_frame_faults() == []


def test_status_reports_armed_faults(permit: None) -> None:
    registry = fi.get_fault_injection_registry()
    registry.arm(fi.WriteFaultKind.THROTTLE_429, target="bot-a", count=3)

    status = registry.status()

    assert status["permitted"] is True
    assert status["write"] == [{"kind": "throttle_429", "target": "bot-a", "remaining": 3, "params": {}}]
    assert status["frame"] == []


# ── Write-fault target matching ───────────────────────────────────────────────


def test_write_fault_matches_target_substring(permit: None) -> None:
    registry = fi.get_fault_injection_registry()
    registry.arm(fi.WriteFaultKind.CONFLICT_409, target="bot-alpha")

    # A non-matching identity leaves the fault armed.
    assert registry.consume_write_fault("manual/bot-beta/v1:xyz") is None
    # The matching identity spends it.
    spent = registry.consume_write_fault("manual/bot-alpha/v1:xyz")
    assert spent is not None
    assert spent.kind == fi.WriteFaultKind.CONFLICT_409


def test_write_fault_none_target_matches_any(permit: None) -> None:
    registry = fi.get_fault_injection_registry()
    registry.arm(fi.WriteFaultKind.TIMEOUT)

    assert registry.consume_write_fault("whatever") is not None


# ── craft_write_error: real map_api_error classification ──────────────────────


def test_reject_422_crafts_request_invalid() -> None:
    error = fi.craft_write_error(fi.WriteFaultKind.REJECT_422)

    assert isinstance(error, BrokerRequestInvalid)
    assert error.http_status == 400


def test_conflict_409_crafts_definitive_order_rejected() -> None:
    error = fi.craft_write_error(fi.WriteFaultKind.CONFLICT_409)

    assert isinstance(error, BrokerOrderRejected)
    assert not isinstance(error, BrokerUnavailable)
    assert error.http_status == 409


def test_throttle_429_crafts_rate_limited_with_zero_retry_after() -> None:
    error = fi.craft_write_error(fi.WriteFaultKind.THROTTLE_429)

    assert isinstance(error, BrokerRateLimited)
    assert error.retry_after_ms == 0


def test_timeout_crafts_broker_unavailable_timed_out() -> None:
    error = fi.craft_write_error(fi.WriteFaultKind.TIMEOUT)

    assert isinstance(error, BrokerUnavailable)
    assert "timed out" in error.message


def test_post_sdk_timeout_crafts_landed_response_loss() -> None:
    error = fi.craft_write_error(fi.WriteFaultKind.POST_SDK_TIMEOUT)

    assert isinstance(error, BrokerUnavailable)
    assert "landed" in (error.detail or "")


# ── craft_frame: valid trade_updates frames ───────────────────────────────────


def test_drain_returns_one_entry_per_remaining_unit(permit: None) -> None:
    registry = fi.get_fault_injection_registry()
    registry.arm(fi.FrameFaultKind.HALT_SUSPENDED, target="coid-1", count=2)

    drained = registry.drain_frame_faults()

    assert len(drained) == 2
    # Draining spends them.
    assert registry.drain_frame_faults() == []


@pytest.mark.parametrize(
    ("kind", "expected_event"),
    [
        (fi.FrameFaultKind.PARTIAL_FILL, "partial_fill"),
        (fi.FrameFaultKind.HALT_SUSPENDED, "suspended"),
        (fi.FrameFaultKind.HALT_STOPPED, "stopped"),
    ],
)
def test_craft_frame_shapes_a_valid_trade_update(kind: str, expected_event: str) -> None:
    import json

    fault = fi.ArmedFault(kind=kind, target="manual/bot-a/v1:coid", remaining=1, params={"symbol": "AAPL"})

    frame = json.loads(fi.craft_frame(fault))

    assert frame["stream"] == "trade_updates"
    assert frame["data"]["event"] == expected_event
    assert frame["data"]["order"]["client_order_id"] == "manual/bot-a/v1:coid"
    assert frame["data"]["order"]["symbol"] == "AAPL"


def test_craft_frame_partial_fill_carries_execution_slice() -> None:
    import json

    fault = fi.ArmedFault(
        kind=fi.FrameFaultKind.PARTIAL_FILL,
        target="coid-1",
        remaining=1,
        params={
            "broker_order_id": "broker-order-123",
            "qty": "3",
            "price": "135.80",
        },
    )

    data = json.loads(fi.craft_frame(fault))["data"]

    assert data["qty"] == "3"
    assert data["price"] == "135.80"
    assert data["execution_id"]
    assert data["order"]["id"] == "broker-order-123"


def test_craft_frame_rejects_redeliver_kind() -> None:
    fault = fi.ArmedFault(kind=fi.FrameFaultKind.REDELIVER_LAST, target=None, remaining=1)

    with pytest.raises(ValueError, match="not a craftable frame fault"):
        fi.craft_frame(fault)
