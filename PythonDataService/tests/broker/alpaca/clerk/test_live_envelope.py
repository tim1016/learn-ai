"""The live risk envelope's values, agreement, cash rule and loss rule."""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.live_envelope import (
    ENVELOPE_ADMISSION_REASON_CODES,
    ENVELOPE_SETTINGS_FIELDS,
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
from app.broker.alpaca.config import _LIVE_REQUIRED_FIELDS, AlpacaSettings
from tests.broker.alpaca.clerk.live_envelope_fixtures import TEST_ENVELOPE_VALUES


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
    same = LiveEnvelopeValues(**TEST_ENVELOPE_VALUES.to_mapping())
    assert same.sha == TEST_ENVELOPE_VALUES.sha
    assert len(TEST_ENVELOPE_VALUES.sha) == 64
    changed = LiveEnvelopeValues(**{**TEST_ENVELOPE_VALUES.to_mapping(), "loss_usd": 5_000.01})
    assert changed.sha != TEST_ENVELOPE_VALUES.sha


def test_from_settings_reads_every_live_value_and_names_the_missing_ones() -> None:
    settings = AlpacaSettings(
        api_key_id="k",
        api_secret_key="s",
        mode="live",
        live_loss_fraction=0.05,
        live_loss_usd=5_000.0,
        live_shadow_sessions=1,
        live_arming_max_sessions=20,
        live_xh_entry_bps=10.0,
        live_xh_exit_bps=10.0,
    )
    assert LiveEnvelopeValues.from_settings(settings) == TEST_ENVELOPE_VALUES
    paper = AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper")
    with pytest.raises(LiveEnvelopeIncomplete, match="live_loss_fraction"):
        LiveEnvelopeValues.from_settings(paper)


def test_agreement_is_unsealed_agreed_or_disagreed() -> None:
    assert envelope_agreement(TEST_ENVELOPE_VALUES, None) == "unsealed"
    assert envelope_agreement(TEST_ENVELOPE_VALUES, LiveEnvelopeValues(**TEST_ENVELOPE_VALUES.to_mapping())) == "agreed"
    other = LiveEnvelopeValues(**{**TEST_ENVELOPE_VALUES.to_mapping(), "loss_fraction": 0.04})
    assert envelope_agreement(TEST_ENVELOPE_VALUES, other) == "disagreed"


def test_the_loss_limit_is_the_tighter_of_fraction_and_usd() -> None:
    assert loss_limit_usd(TEST_ENVELOPE_VALUES, last_equity_usd=100_000.0) == pytest.approx(5_000.0)
    assert loss_limit_usd(TEST_ENVELOPE_VALUES, last_equity_usd=40_000.0) == pytest.approx(2_000.0)
    assert loss_breached(day_pnl_usd=-2_000.0, loss_limit_usd=2_000.0)
    assert not loss_breached(day_pnl_usd=-1_999.99, loss_limit_usd=2_000.0)


def test_the_cash_rule_counts_the_new_order_and_working_reservations_only() -> None:
    assert cash_bound_admits(cash_available_usd=10_000.0, reserved_usd=0.0, notional_usd=10_000.0)
    assert not cash_bound_admits(cash_available_usd=10_000.0, reserved_usd=0.01, notional_usd=10_000.0)
    assert cash_bound_admits(cash_available_usd=10_000.0, reserved_usd=4_000.0, notional_usd=6_000.0)
    assert EnvelopeReservation(quantity=10, reference_price=12.5).notional_usd == pytest.approx(125.0)


def test_the_gate_serves_only_a_fresh_observation() -> None:
    gate = LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=True)
    assert gate.agreement == "unsealed"
    assert gate.latest_observation() is None
    assert gate.fresh_observation(1_000) is None
    gate.publish(_observation(1_000))
    assert gate.fresh_observation(1_000 + OBSERVATION_MAX_AGE_MS) is not None
    assert gate.fresh_observation(1_000 + OBSERVATION_MAX_AGE_MS + 1) is None
    assert gate.latest_observation() == _observation(1_000)


def test_an_observation_dated_after_the_clock_is_not_fresh() -> None:
    """A backward clock step makes the age negative, not the observation new.

    Left as ``age > max_age``, a rollback would serve an obsolete observation
    indefinitely — the one window where ENTERs bound against stale cash and
    the 45 s limit says nothing.
    """
    gate = LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=True)
    gate.publish(_observation(1_000))
    assert gate.fresh_observation(1_000 - 1) is None
    assert gate.fresh_observation(1_000) is not None


def test_withdrawing_drops_the_observation_at_once() -> None:
    """An account the sync could not judge refuses every ENTER now, not once stale."""
    gate = LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=True)
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


def test_the_envelope_reads_exactly_the_settings_live_mode_requires() -> None:
    """A value added to one list only would turn a valid live boot into a service that fails to start."""
    assert tuple(name for _, name in ENVELOPE_SETTINGS_FIELDS) == tuple(_LIVE_REQUIRED_FIELDS)
