"""The live risk envelope's values, agreement, cash rule and loss rule."""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.live_envelope import (
    ENVELOPE_ADMISSION_REASON_CODES,
    OBSERVATION_MAX_AGE_MS,
    AccountObservation,
    EnvelopeReservation,
    LiveEnvelopeGate,
    LiveEnvelopeIncomplete,
    LiveEnvelopeValues,
    cash_bound_admits,
    envelope_agreement,
    loss_breached,
    loss_limit_usd,
)
from app.broker.alpaca.config import AlpacaSettings

VALUES = LiveEnvelopeValues(
    loss_fraction=0.05,
    loss_usd=5_000.0,
    shadow_sessions=3,
    arming_max_sessions=20,
    xh_entry_bps=10.0,
    xh_exit_bps=10.0,
)


def _observation(observed_at_ms: int, *, cash: float = 100_000.0) -> AccountObservation:
    return AccountObservation(
        observed_at_ms=observed_at_ms,
        broker_cash_usd=cash,
        cash_available_usd=cash,
        last_equity_usd=100_000.0,
        unrealized_pl_usd=0.0,
        position_count=0,
    )


def test_the_sha_is_stable_and_changes_with_any_value() -> None:
    same = LiveEnvelopeValues(**VALUES.to_mapping())
    assert same.sha == VALUES.sha
    assert len(VALUES.sha) == 64
    changed = LiveEnvelopeValues(**{**VALUES.to_mapping(), "loss_usd": 5_000.01})
    assert changed.sha != VALUES.sha


def test_from_settings_reads_every_live_value_and_names_the_missing_ones() -> None:
    settings = AlpacaSettings(
        api_key_id="k",
        api_secret_key="s",
        mode="live",
        live_loss_fraction=0.05,
        live_loss_usd=5_000.0,
        live_shadow_sessions=3,
        live_arming_max_sessions=20,
        live_xh_entry_bps=10.0,
        live_xh_exit_bps=10.0,
    )
    assert LiveEnvelopeValues.from_settings(settings) == VALUES
    paper = AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper")
    with pytest.raises(LiveEnvelopeIncomplete, match="live_loss_fraction"):
        LiveEnvelopeValues.from_settings(paper)


def test_agreement_is_unsealed_agreed_or_disagreed() -> None:
    assert envelope_agreement(VALUES, None) == "unsealed"
    assert envelope_agreement(VALUES, LiveEnvelopeValues(**VALUES.to_mapping())) == "agreed"
    other = LiveEnvelopeValues(**{**VALUES.to_mapping(), "loss_fraction": 0.04})
    assert envelope_agreement(VALUES, other) == "disagreed"


def test_the_loss_limit_is_the_tighter_of_fraction_and_usd() -> None:
    assert loss_limit_usd(VALUES, last_equity_usd=100_000.0) == pytest.approx(5_000.0)
    assert loss_limit_usd(VALUES, last_equity_usd=40_000.0) == pytest.approx(2_000.0)
    assert loss_breached(day_pnl_usd=-2_000.0, loss_limit_usd=2_000.0)
    assert not loss_breached(day_pnl_usd=-1_999.99, loss_limit_usd=2_000.0)


def test_the_cash_rule_counts_the_new_order_and_working_reservations_only() -> None:
    assert cash_bound_admits(cash_available_usd=10_000.0, reserved_usd=0.0, notional_usd=10_000.0)
    assert not cash_bound_admits(cash_available_usd=10_000.0, reserved_usd=0.01, notional_usd=10_000.0)
    assert cash_bound_admits(cash_available_usd=10_000.0, reserved_usd=4_000.0, notional_usd=6_000.0)
    assert EnvelopeReservation(quantity=10, reference_price=12.5).notional_usd == pytest.approx(125.0)


def test_the_gate_serves_only_a_fresh_observation() -> None:
    gate = LiveEnvelopeGate(values=VALUES, custody_is_simulated=True)
    assert gate.agreement == "unsealed"
    assert gate.latest_observation() is None
    assert gate.fresh_observation(1_000) is None
    gate.publish(_observation(1_000))
    assert gate.fresh_observation(1_000 + OBSERVATION_MAX_AGE_MS) is not None
    assert gate.fresh_observation(1_000 + OBSERVATION_MAX_AGE_MS + 1) is None
    assert gate.latest_observation() == _observation(1_000)


def test_withdrawing_drops_the_observation_at_once() -> None:
    """An account the sync could not judge refuses every ENTER now, not once stale."""
    gate = LiveEnvelopeGate(values=VALUES, custody_is_simulated=True)
    gate.publish(_observation(1_000))
    assert gate.fresh_observation(1_000) is not None
    gate.withdraw()
    assert gate.fresh_observation(1_000) is None
    assert gate.latest_observation() is None


def test_the_admission_reason_codes_are_the_four_envelope_refusals() -> None:
    assert set(ENVELOPE_ADMISSION_REASON_CODES) == {
        "LIVE_ENVELOPE_CASH_EXCEEDED",
        "LIVE_ENVELOPE_LOSS_HOLD",
        "LIVE_ENVELOPE_DISAGREEMENT",
        "LIVE_ENVELOPE_UNOBSERVED",
    }
    assert isinstance(ENVELOPE_ADMISSION_REASON_CODES, frozenset)
