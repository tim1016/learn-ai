"""Quantity copy and cause instants keep the same precise domain boundaries."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.broker.alpaca.clerk.sqlite import manual_order_completion
from app.broker.alpaca.clerk.sqlite.flatten_cover import FLATTEN_COVER_UNPROVEN, flatten_cover_refusal
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import ExitStuckCause, LossHoldCause, MissedScheduledClose
from app.broker.contract.models import OrderSide
from app.utils.session_anchors import MAX_TIMESTAMP_MS


@pytest.mark.parametrize(
    ("order_quantity", "filled_quantity", "expected"),
    [
        (123456789.12345679, 0.123456789, "Cancelled at Alpaca with 0.123456789 of 123456789.12345679 shares filled."),
        (5.0, 1.0000000005, "Cancelled at Alpaca with 1 of 5 shares filled."),
    ],
)
def test_manual_order_ending_uses_the_exact_share_formatter(
    monkeypatch: pytest.MonkeyPatch, order_quantity: float, filled_quantity: float, expected: str
) -> None:
    monkeypatch.setattr(
        manual_order_completion,
        "accepted_manual_leg",
        lambda _repo, *, order_ref: SimpleNamespace(quantity=order_quantity),
    )
    repo = SimpleNamespace(
        manual_order_cancellation=lambda *, order_ref: None,
        effective_fill_totals_for_order=lambda order_ref: (filled_quantity, None),
    )
    order = SimpleNamespace(order_ref="order-1", broker_state="canceled")

    assert manual_order_completion.manual_order_ending_copy(repo, order=order) == expected


def test_flatten_refusal_names_the_exact_fractional_position() -> None:
    refusal = flatten_cover_refusal(
        symbol="SPY", side=OrderSide.SELL, quantity=1234567.123456789, observed=None
    )

    assert refusal is not None
    assert refusal.reason_code == FLATTEN_COVER_UNPROVEN
    assert "holds the 1234567.123456789 SPY this flatten would sell" in refusal.explanation


@pytest.mark.parametrize(
    ("cause", "field"),
    [
        (ExitStuckCause("SPY", 1.0, 0, 1), "first_observed_at_ms"),
        (MissedScheduledClose("SPY", 1.0, "expiry", 1, 1), "due_ms"),
        (MissedScheduledClose("SPY", 1.0, "expiry", 1, 1), "session_open_ms"),
        (LossHoldCause(1, -1.0, 2.0, 100.0, 1), "day_start_ms"),
        (LossHoldCause(1, -1.0, 2.0, 100.0, 1), "observed_at_ms"),
    ],
)
def test_uncertainty_cause_instants_use_the_domain_ceiling(cause: object, field: str) -> None:
    mapping = cause.to_mapping()
    for accepted in (0, MAX_TIMESTAMP_MS):
        assert cause.__class__.from_mapping({**mapping, field: accepted}).to_mapping()[field] == accepted
    for rejected in (-1, MAX_TIMESTAMP_MS + 1, True, 1.5):
        with pytest.raises(ValueError, match="ms"):
            cause.__class__.from_mapping({**mapping, field: rejected})
