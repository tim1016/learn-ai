"""Where an extended-session leg's allowance comes from (ADR 0060; plan §0 D3).

The rule under test: **exits price from the newest armed record's sealed
envelope**, fall back to the effective revision when no armed record is
readable, and are never blocked by a broker-configuration refusal.

Before this, ``ExtendedHoursAllowances.from_environment()`` read the process
settings, so an operator who raised ``ALPACA_LIVE_XH_EXIT_BPS`` and restarted
changed *exit* pricing without the arming ceremony that is the only act allowed
to make a new live limit binding.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import asdict, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr

from app.broker.alpaca.active_binding import (
    BROKER_UNCONFIGURED,
    UnboundBroker,
    refuse_active_alpaca_binding,
    reset_active_alpaca_binding_for_testing,
    set_active_alpaca_binding,
)
from app.broker.alpaca.broker import ALPACA_LIVE_CAPABILITIES
from app.broker.alpaca.clerk.live_arming import LiveArmingRecord
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.clerk.program_leg import (
    ProgramLegPolicy,
    ProgramLegRefused,
    resolve_extended_hours_allowances,
    shape_program_leg,
)
from app.broker.alpaca.config import AlpacaSettings, reset_alpaca_settings_for_testing
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.broker.alpaca.profile.credentials import ResolvedCredentials
from app.broker.alpaca.profile.runtime_context import AlpacaRuntimeContext
from app.broker.contract.capabilities import BrokerCapabilities, ExtendedHoursWindow
from app.broker.contract.models import OrderSide, OrderType
from app.services.source_bar_ledger import RetainedSourceBar
from app.utils.timestamps import to_ms_utc

_ET = ZoneInfo("America/New_York")
_DAY = date(2026, 9, 2)
_WINDOW = ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)
_ACCOUNT = "9LIVE0001"
_SID = "ema-live-1"
_ARMED_AT_MS = 1_757_000_000_000

# The seal and the revision differ in exactly the two bps fields, so a passing
# assertion can only be explained by *which document* was read.
_SEALED = LiveEnvelopeValues(
    loss_fraction=0.05,
    loss_usd=5_000.0,
    shadow_sessions=1,
    arming_max_sessions=20,
    xh_entry_bps=10.0,
    xh_exit_bps=20.0,
)
_REVISION = replace(_SEALED, xh_entry_bps=77.0, xh_exit_bps=99.0)

_SEALED_ALLOWANCES = ExtendedHoursAllowances(entry_bps=Decimal("10"), exit_bps=Decimal("20"))
_REVISION_ALLOWANCES = ExtendedHoursAllowances(entry_bps=Decimal("77"), exit_bps=Decimal("99"))


@pytest.fixture(autouse=True)
def _no_binding_leaks_between_tests() -> Iterator[None]:
    """Both process-wide holders this resolution reads are per-test state."""
    reset_active_alpaca_binding_for_testing()
    reset_alpaca_settings_for_testing()
    yield
    reset_active_alpaca_binding_for_testing()
    reset_alpaca_settings_for_testing()


def _settings(*, clerk_dir: Path, envelope: LiveEnvelopeValues | None) -> AlpacaSettings:
    live = {} if envelope is None else {f"live_{name}": value for name, value in asdict(envelope).items()}
    return AlpacaSettings(
        api_key_id="fixture-key-not-a-real-credential",
        api_secret_key="fixture-secret-not-a-real-credential",
        clerk_dir=clerk_dir,
        mode="paper" if envelope is None else "live",
        **live,
    )


def _bind(
    *,
    clerk_dir: Path,
    envelope: LiveEnvelopeValues | None = _REVISION,
    account_pin: str | None = _ACCOUNT,
) -> AlpacaRuntimeContext:
    """Install one resolved binding, the way a bound worker holds it."""
    context = AlpacaRuntimeContext(
        settings=_settings(clerk_dir=clerk_dir, envelope=envelope),
        credentials=ResolvedCredentials(
            slot="default",
            api_key_id=SecretStr("fixture-key-not-a-real-credential"),
            api_secret_key=SecretStr("fixture-secret-not-a-real-credential"),
        ),
        live_envelope=envelope,
        account_pin=account_pin,
        profile_id="profile-1",
        revision=1,
    )
    set_active_alpaca_binding(context)
    return context


def _arm(clerk_dir: Path, envelope: LiveEnvelopeValues = _SEALED, *, instance: str = _SID) -> None:
    LiveArmingLedger(clerk_dir, live_account_id=_ACCOUNT).append(
        LiveArmingRecord.create(
            live_account_id=_ACCOUNT,
            strategy_instance_id=instance,
            seal_hash="a" * 64,
            configured_signal_hash="b" * 64,
            shadow_receipt_sha256=None,
            envelope=envelope,
            armed_at_ms=_ARMED_AT_MS,
            max_sessions=envelope.arming_max_sessions,
        )
    )


def _bar(hour: int, minute: int, *, close: str = "100.00") -> RetainedSourceBar:
    end = to_ms_utc(datetime(_DAY.year, _DAY.month, _DAY.day, hour, minute, tzinfo=_ET))
    return RetainedSourceBar(
        seq=1,
        account_id=_ACCOUNT,
        provider="alpaca",
        symbol="SPY",
        bar_identity=f"alpaca:SPY:{end - 60_000}:{end}",
        bar_ref="bar-1",
        start_ms=end - 60_000,
        end_ms=end,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=1,
        fetched_at_ms=end,
        session_phase="UNKNOWN",
    )


class _ExtendedHoursReadPort:
    """Only what ``from_read_port`` asks of a read port."""

    def capabilities(self) -> BrokerCapabilities:
        return ALPACA_LIVE_CAPABILITIES.revised(
            supports_extended_hours=True, extended_hours_window=_WINDOW
        )


def test_the_sealed_envelope_outranks_the_effective_revision(tmp_path: Path) -> None:
    """The whole decision: what the operator *armed* prices the leg."""
    _bind(clerk_dir=tmp_path)
    _arm(tmp_path)

    assert resolve_extended_hours_allowances() == _SEALED_ALLOWANCES


def test_the_newest_arming_reseals_both_allowances(tmp_path: Path) -> None:
    _bind(clerk_dir=tmp_path)
    _arm(tmp_path)
    _arm(tmp_path, replace(_SEALED, xh_entry_bps=30.0, xh_exit_bps=40.0), instance="ema-live-2")

    assert resolve_extended_hours_allowances() == ExtendedHoursAllowances(
        entry_bps=Decimal("30"), exit_bps=Decimal("40")
    )


def test_a_disarm_does_not_unseal_the_allowance(tmp_path: Path) -> None:
    """A disarm row carries no envelope, so it never becomes the pricing document."""
    _bind(clerk_dir=tmp_path)
    _arm(tmp_path)
    LiveArmingLedger(tmp_path, live_account_id=_ACCOUNT).revoke_latest(
        _SID, disarmed_at_ms=_ARMED_AT_MS + 1_000
    )

    assert resolve_extended_hours_allowances() == _SEALED_ALLOWANCES


def test_the_effective_revision_prices_when_no_arming_exists(tmp_path: Path) -> None:
    _bind(clerk_dir=tmp_path)

    assert resolve_extended_hours_allowances() == _REVISION_ALLOWANCES


def test_an_unpinned_revision_has_no_account_whose_seal_to_read(tmp_path: Path) -> None:
    """Without an observed account there is no ledger to consult; the revision decides."""
    _arm(tmp_path)
    _bind(clerk_dir=tmp_path, account_pin=None)

    assert resolve_extended_hours_allowances() == _REVISION_ALLOWANCES


def test_an_unreadable_arming_ledger_never_blocks_pricing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A damaged ledger is reported loudly and falls through — it does not raise."""
    _bind(clerk_dir=tmp_path)
    ledger = LiveArmingLedger(tmp_path, live_account_id=_ACCOUNT)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text("this is not a sealed arming row\n")

    with caplog.at_level(logging.ERROR):
        allowances = resolve_extended_hours_allowances()

    assert allowances == _REVISION_ALLOWANCES
    assert any(
        record.__dict__.get("action") == "extended_hours_allowances_seal_unreadable"
        for record in caplog.records
    )


def test_a_refused_binding_never_propagates_out_of_pricing() -> None:
    """``resolved_alpaca_settings`` raises ``BrokerUnbound``; pricing must not."""
    refuse_active_alpaca_binding(
        UnboundBroker(
            reason=BROKER_UNCONFIGURED,
            message="No broker profile is effective for this worker.",
            next_step="Select a profile and press Apply.",
        )
    )

    assert resolve_extended_hours_allowances() is None
    assert ProgramLegPolicy.from_read_port(_ExtendedHoursReadPort()).allowances is None


def test_a_refused_binding_refuses_an_exit_as_a_typed_leg_refusal() -> None:
    """The Clerk turns this into a rejected receipt; a ``BrokerUnbound`` would kill the task.

    With no seal, no revision and no settings there is genuinely no number, so
    the pre-existing ``EXTENDED_HOURS_ALLOWANCE_UNSET`` stands — for an EXIT as
    much as an ENTER. What must never happen is the configuration refusal
    escaping as itself: a number nobody chose must not bound real money either
    (ADR 0059 D4).
    """
    refuse_active_alpaca_binding(
        UnboundBroker(reason=BROKER_UNCONFIGURED, message="unconfigured", next_step="apply a profile")
    )
    policy = ProgramLegPolicy.from_read_port(_ExtendedHoursReadPort())

    with pytest.raises(ProgramLegRefused) as refused:
        shape_program_leg(
            side=OrderSide.SELL,
            purpose=EffectPurpose.EXIT,
            use_rth=False,
            decision_bar=_bar(8, 0),
            policy=policy,
        )

    assert refused.value.reason_code == "EXTENDED_HOURS_ALLOWANCE_UNSET"


def test_an_exit_leg_is_anchored_by_the_sealed_allowance_not_the_revision(tmp_path: Path) -> None:
    """End to end: the sealed 20 bps prices the exit, not the revision's 99 bps."""
    _bind(clerk_dir=tmp_path)
    _arm(tmp_path)
    policy = ProgramLegPolicy.from_read_port(_ExtendedHoursReadPort())

    shape = shape_program_leg(
        side=OrderSide.SELL,
        purpose=EffectPurpose.EXIT,
        use_rth=False,
        decision_bar=_bar(8, 0),
        policy=policy,
    )

    assert shape.order_type is OrderType.LIMIT
    assert shape.extended_hours is True
    # 20 bps below 100.00, floored to the penny tick; 99 bps would be 99.01.
    assert shape.limit_price == pytest.approx(99.80, abs=1e-9)


def test_an_entry_leg_is_anchored_by_the_sealed_allowance_too(tmp_path: Path) -> None:
    _bind(clerk_dir=tmp_path)
    _arm(tmp_path)
    policy = ProgramLegPolicy.from_read_port(_ExtendedHoursReadPort())

    shape = shape_program_leg(
        side=OrderSide.BUY,
        purpose=EffectPurpose.ENTER,
        use_rth=False,
        decision_bar=_bar(8, 0),
        policy=policy,
    )

    # 10 bps above 100.00; the revision's 77 bps would be 100.77.
    assert shape.limit_price == pytest.approx(100.10, abs=1e-9)


def test_the_process_environment_only_answers_when_nothing_has_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug, stated as a ladder.

    Raising ``ALPACA_LIVE_XH_EXIT_BPS`` and restarting reaches pricing only
    while no binding has been attempted at all — the pre-ADR-0060 world this
    resolution's last step preserves. A bound worker answers from its revision,
    and an armed one from its seal; neither reads the environment again.
    """
    monkeypatch.setenv("ALPACA_API_KEY_ID", "fixture-key-not-a-real-credential")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "fixture-secret-not-a-real-credential")
    monkeypatch.setenv("ALPACA_MODE", "paper")
    monkeypatch.setenv("ALPACA_LIVE_XH_ENTRY_BPS", "1")
    monkeypatch.setenv("ALPACA_LIVE_XH_EXIT_BPS", "2")

    assert resolve_extended_hours_allowances() == ExtendedHoursAllowances(
        entry_bps=Decimal("1"), exit_bps=Decimal("2")
    )

    _bind(clerk_dir=tmp_path)
    assert resolve_extended_hours_allowances() == _REVISION_ALLOWANCES

    _arm(tmp_path)
    assert resolve_extended_hours_allowances() == _SEALED_ALLOWANCES


def test_from_read_port_takes_the_resolver_as_a_seam() -> None:
    """The composition sites keep the production default; a test can state the document."""
    stated = ExtendedHoursAllowances(entry_bps=Decimal("3"), exit_bps=Decimal("4"))

    policy = ProgramLegPolicy.from_read_port(_ExtendedHoursReadPort(), allowances=lambda: stated)

    assert policy.allowances == stated
    assert policy.window == _WINDOW
