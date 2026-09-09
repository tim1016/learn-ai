"""The Alpaca live verdict is a pure function of settings and clerk selection
(ADR 0059 D8). It never contacts the broker and never guesses."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    ClerkStartupFailure,
)
from app.broker.alpaca.clerk.live_arming import LiveArmingRecord
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.config import AlpacaSettings
from app.schemas.alpaca_live_verdict import ShadowState
from app.services.alpaca_live_verdict import (
    ArmingObservation,
    alpaca_live_verdict,
    observe_arming,
    observe_loss_hold,
)
from app.services.session_authority import et_minute_of_day_ms
from tests.broker.alpaca.clerk.live_arming_fixtures import (
    ARMED_AT_MS,
    ARMING_SID,
    arming_ready,
    live_settings,
    paper_settings,
)
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT, TEST_ENVELOPE_VALUES
from tests.broker.alpaca.clerk.test_shadow_envelope_runtime import shadow_runtime  # noqa: F401

_NOW = 1_800_000_000_000
_LIVE = {
    "live_loss_fraction": 0.02, "live_loss_usd": 500.0, "live_shadow_sessions": 5,
    "live_arming_max_sessions": 20, "live_xh_entry_bps": 10.0, "live_xh_exit_bps": 10.0,
}


def _paper() -> AlpacaSettings:
    return AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper")


def _live() -> AlpacaSettings:
    return AlpacaSettings(api_key_id="k", api_secret_key="s", mode="live", **_LIVE)


def _failure(reason_code: str, account_id: str | None) -> ActiveClerkRuntime:
    return ActiveClerkRuntime(
        authority_kind="unavailable",
        account_id=account_id,
        startup_failure=ClerkStartupFailure(
            reason_code=reason_code,
            account_id=account_id,
            scope="ACCOUNT_CLERK",
            impact="x",
            recovery="y",
            observed_at_ms=_NOW,
        ),
    )


def test_unconfigured_settings_is_unknown() -> None:
    verdict = alpaca_live_verdict(settings=None, runtime=None, now_ms=_NOW)

    assert verdict.configured_mode == "unconfigured"
    assert verdict.final_verdict == "unknown"
    assert verdict.observed_at_ms == _NOW


def test_paper_settings_is_paper_regardless_of_clerk_state() -> None:
    verdict = alpaca_live_verdict(settings=_paper(), runtime=None, now_ms=_NOW)

    assert verdict.configured_mode == "paper"
    assert verdict.final_verdict == "paper"
    assert verdict.envelope_state == "not_applicable"
    assert verdict.armed_instance_count == 0


def test_live_with_refused_clerk_is_live_unarmed_and_names_the_account() -> None:
    runtime = _failure("LIVE_ACCOUNT_REFUSED", "9LIVE0001")

    verdict = alpaca_live_verdict(settings=_live(), runtime=runtime, now_ms=_NOW)

    assert verdict.configured_mode == "live"
    assert verdict.observed_account_id == "9LIVE0001"
    assert verdict.mode_agreement == "agreed"
    assert verdict.clerk_refusal_reason_code == "LIVE_ACCOUNT_REFUSED"
    assert verdict.envelope_state == "configured_unsealed"
    assert verdict.final_verdict == "live-unarmed"
    assert "9LIVE0001" in verdict.headline


def test_live_with_mode_disagreement_is_unknown_and_disagreed() -> None:
    runtime = _failure("LIVE_MODE_DISAGREEMENT", None)

    verdict = alpaca_live_verdict(settings=_live(), runtime=runtime, now_ms=_NOW)

    assert verdict.mode_agreement == "disagreed"
    assert verdict.final_verdict == "unknown"


def test_live_with_no_account_observed_is_unknown_and_unobserved() -> None:
    runtime = _failure("BROKER_ACCOUNT_UNAVAILABLE", None)

    verdict = alpaca_live_verdict(settings=_live(), runtime=runtime, now_ms=_NOW)

    assert verdict.mode_agreement == "unobserved"
    assert verdict.final_verdict == "unknown"


def test_live_with_no_runtime_yet_is_unknown() -> None:
    verdict = alpaca_live_verdict(settings=_live(), runtime=None, now_ms=_NOW)

    assert verdict.clerk_authority == "not_installed"
    assert verdict.final_verdict == "unknown"


@pytest.mark.parametrize("final", ["paper", "live-unarmed", "unknown"])
def test_every_verdict_carries_server_authored_copy(final: str) -> None:
    settings, runtime = {
        "paper": (_paper(), None),
        "live-unarmed": (_live(), _failure("LIVE_ACCOUNT_REFUSED", "9LIVE0001")),
        "unknown": (None, None),
    }[final]

    verdict = alpaca_live_verdict(settings=settings, runtime=runtime, now_ms=_NOW)

    assert verdict.final_verdict == final
    assert verdict.headline and verdict.detail


def test_paper_with_mode_disagreement_is_unknown_not_reassuring() -> None:
    runtime = _failure("LIVE_MODE_DISAGREEMENT", None)

    verdict = alpaca_live_verdict(settings=_paper(), runtime=runtime, now_ms=_NOW)

    assert verdict.configured_mode == "paper"
    assert verdict.mode_agreement == "disagreed"
    assert verdict.final_verdict == "unknown"
    assert "no real money" not in verdict.headline
    assert verdict.clerk_refusal_reason_code == "LIVE_MODE_DISAGREEMENT"


def test_clean_paper_selection_is_the_normal_path() -> None:
    runtime = ActiveClerkRuntime(authority_kind="sqlite", account_id="PA0SANITIZED00001")

    verdict = alpaca_live_verdict(settings=_paper(), runtime=runtime, now_ms=_NOW)

    assert verdict.clerk_authority == "sqlite"
    assert verdict.clerk_refusal_reason_code is None
    assert verdict.mode_agreement == "agreed"
    assert verdict.final_verdict == "paper"
    assert "PA0SANITIZED00001" in verdict.headline


def _shadow_runtime() -> ActiveClerkRuntime:
    return ActiveClerkRuntime(authority_kind="shadow", account_id="shadow:9LIVE0001", account_authority_kind="shadow")


_SHADOW_STATES: tuple[ShadowState, ...] = ("none", "in_progress", "complete")


@pytest.mark.parametrize("shadow_state", _SHADOW_STATES)
def test_live_shadow_authority_reports_the_observed_shadow_state(shadow_state: ShadowState) -> None:
    verdict = alpaca_live_verdict(settings=_live(), runtime=_shadow_runtime(), now_ms=_NOW, shadow_state=shadow_state)

    assert verdict.clerk_authority == "shadow"
    assert verdict.observed_account_id == "shadow:9LIVE0001"
    assert verdict.mode_agreement == "agreed"
    assert verdict.final_verdict == "live-unarmed"
    assert verdict.shadow_state == shadow_state
    assert "shadow authority" in verdict.headline
    # The headline names the LIVE account a human recognises; ``shadow:`` is a
    # runtime custody namespace, not part of the account number.
    assert "LIVE account 9LIVE0001 " in verdict.headline
    assert "shadow:" not in verdict.headline


def test_shadow_state_is_not_applicable_on_paper_even_if_supplied() -> None:
    verdict = alpaca_live_verdict(settings=_paper(), runtime=None, now_ms=_NOW, shadow_state="complete")

    assert verdict.shadow_state == "not_applicable"


def test_clerk_authority_literal_tracks_the_runtime_kind() -> None:
    from typing import get_args

    from app.broker.alpaca.clerk.active_authority import AuthorityKind as RuntimeAuthorityKind
    from app.schemas.alpaca_live_verdict import ClerkAuthority

    assert set(get_args(ClerkAuthority)) == set(get_args(RuntimeAuthorityKind)) | {"not_installed"}


def test_observed_at_ms_is_bounded_to_the_canonical_epoch_range() -> None:
    from pydantic import ValidationError

    from app.schemas.alpaca_live_verdict import AlpacaLiveVerdict
    from app.utils.session_anchors import MAX_TIMESTAMP_MS

    base = alpaca_live_verdict(settings=_paper(), runtime=None, now_ms=_NOW).model_dump()

    AlpacaLiveVerdict(**{**base, "observed_at_ms": MAX_TIMESTAMP_MS})
    with pytest.raises(ValidationError):
        AlpacaLiveVerdict(**{**base, "observed_at_ms": MAX_TIMESTAMP_MS + 1})


def test_paper_reports_the_envelope_as_not_applicable() -> None:
    runtime = ActiveClerkRuntime(authority_kind="sqlite", account_id="PA0SANITIZED00001")

    verdict = alpaca_live_verdict(settings=_paper(), runtime=runtime, now_ms=_NOW)

    assert (verdict.envelope_agreement, verdict.loss_hold) == ("not_applicable", "not_applicable")


def test_a_live_account_with_no_envelope_installed_says_not_applicable_and_shows_the_hold() -> None:
    """``_shadow_runtime()`` carries no clerk, so it carries no envelope object.

    That is exactly the shape of a live boot the composition refused
    ``LIVE_ENVELOPE_MISSING``, and it must not read as ``unsealed`` --
    "configured, not yet sealed" is a different and far less alarming thing
    than "no envelope at all". The composed ASGI test pins ``unsealed`` for
    the case where a real gate *is* installed.
    """
    clear = alpaca_live_verdict(
        settings=_live(), runtime=_shadow_runtime(), now_ms=_NOW, shadow_state="none", loss_hold="clear"
    )
    assert (clear.envelope_agreement, clear.loss_hold) == ("not_applicable", "clear")

    held = alpaca_live_verdict(
        settings=_live(), runtime=_shadow_runtime(), now_ms=_NOW, shadow_state="none", loss_hold="held"
    )
    assert held.loss_hold == "held"
    assert "loss hold" in held.headline
    assert "POST /api/brokers/alpaca/live-envelope/loss-hold/clear" in held.detail


async def test_observe_loss_hold_reads_the_durable_hold_on_a_composed_shadow_runtime(
    shadow_runtime: tuple[ActiveClerkRuntime, object],  # noqa: F811 — the imported fixture
) -> None:
    runtime, broker = shadow_runtime
    assert observe_loss_hold(runtime) == "clear"

    broker.unrealized = -5_000.0
    await runtime.envelope_sync.tick()
    assert observe_loss_hold(runtime) == "held"
    assert observe_loss_hold(None) == "not_applicable"


MONDAY_MS = et_minute_of_day_ms(date(2026, 9, 14), 10 * 60)


def _arm_on_disk(
    artifacts_root: Path,
    live_state_root: Path,
    *,
    strategy_instance_id: str = ARMING_SID,
    armed_at_ms: int = ARMED_AT_MS,
    max_sessions: int = 20,
) -> LiveArmingRecord:
    """One sealed instance and one arming record for it, both on disk.

    The sealed envelope grants exactly ``max_sessions``: the record's lapse
    count and the envelope's ``arming_max_sessions`` are one number, so an
    observation of this ledger must be made against settings that say the same.
    """
    seal = arming_ready(artifacts_root, live_state_root, strategy_instance_id=strategy_instance_id)
    record = LiveArmingRecord.create(
        live_account_id=LIVE_ACCT,
        strategy_instance_id=strategy_instance_id,
        seal_hash=seal.bot_configuration_hash,
        configured_signal_hash=seal.configured_signal_hash,
        shadow_receipt_sha256="c" * 64,
        envelope=replace(TEST_ENVELOPE_VALUES, arming_max_sessions=max_sessions),
        armed_at_ms=armed_at_ms,
        max_sessions=max_sessions,
    )
    LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT).append(record)
    return record


def test_an_absent_arming_observation_keeps_the_slice_five_verdict() -> None:
    """Every caller that has not been taught to observe arming still gets the truth it had."""
    verdict = alpaca_live_verdict(settings=_live(), runtime=_shadow_runtime(), now_ms=_NOW, shadow_state="none")

    assert verdict.armed_instance_count == 0
    assert verdict.envelope_state == "configured_unsealed"
    assert verdict.final_verdict == "live-unarmed"


def test_one_armed_instance_makes_the_verdict_live_armed_and_the_envelope_sealed() -> None:
    verdict = alpaca_live_verdict(
        settings=_live(),
        runtime=_shadow_runtime(),
        now_ms=_NOW,
        shadow_state="complete",
        arming=ArmingObservation(armed_instance_count=1, envelope_state="sealed", detail=""),
    )

    assert verdict.armed_instance_count == 1
    assert verdict.envelope_state == "sealed"
    assert verdict.final_verdict == "live-armed"
    assert "1 instance armed" in verdict.headline
    assert "LIVE account 9LIVE0001 " in verdict.headline
    assert "no real-money order" in verdict.detail or "No path submits a real-money order" in verdict.detail
    assert "slice 7" in verdict.detail


def test_the_headline_counts_more_than_one_armed_instance_in_the_plural() -> None:
    verdict = alpaca_live_verdict(
        settings=_live(),
        runtime=_shadow_runtime(),
        now_ms=_NOW,
        shadow_state="complete",
        arming=ArmingObservation(armed_instance_count=3, envelope_state="sealed", detail=""),
    )

    assert "3 instances armed" in verdict.headline


def test_an_armed_count_without_an_installed_clerk_is_never_live_armed() -> None:
    """R11: the verdict is ``live-armed`` only where custody could exist at all."""
    verdict = alpaca_live_verdict(
        settings=_live(),
        runtime=_failure("LIVE_ACCOUNT_REFUSED", "9LIVE0001"),
        now_ms=_NOW,
        arming=ArmingObservation(armed_instance_count=1, envelope_state="sealed", detail=""),
    )

    assert verdict.armed_instance_count == 1
    assert verdict.final_verdict == "live-unarmed"


def test_a_lapsed_or_disarmed_instance_is_named_in_the_detail_with_its_reason_code() -> None:
    verdict = alpaca_live_verdict(
        settings=_live(),
        runtime=_shadow_runtime(),
        now_ms=_NOW,
        shadow_state="complete",
        arming=ArmingObservation(
            armed_instance_count=0,
            envelope_state="sealed",
            detail=" Not armed: s1 (LIVE_ARMING_LAPSED); s2 (LIVE_ARMING_REVOKED).",
        ),
    )

    assert verdict.final_verdict == "live-unarmed"
    assert verdict.envelope_state == "sealed"
    assert "s1 (LIVE_ARMING_LAPSED)" in verdict.detail
    assert "s2 (LIVE_ARMING_REVOKED)" in verdict.detail


def test_paper_stays_untouched_even_when_an_arming_observation_is_supplied() -> None:
    verdict = alpaca_live_verdict(
        settings=_paper(),
        runtime=None,
        now_ms=_NOW,
        arming=ArmingObservation(armed_instance_count=2, envelope_state="sealed", detail=" Not armed: x."),
    )

    assert verdict.final_verdict == "paper"
    assert verdict.armed_instance_count == 0
    assert verdict.envelope_state == "not_applicable"
    assert "Not armed" not in verdict.detail


def test_observe_arming_counts_the_ledgers_armed_instances(tmp_path: Path) -> None:
    artifacts_root, live_state_root = tmp_path / "clerk", tmp_path / "runner"
    _arm_on_disk(artifacts_root, live_state_root)

    observation = observe_arming(
        _shadow_runtime(),
        artifacts_root,
        lambda: live_state_root,
        settings=live_settings(),
        now_ms=ARMED_AT_MS,
    )

    assert observation == ArmingObservation(armed_instance_count=1, envelope_state="sealed", detail="")


def test_observe_arming_names_a_lapsed_instance_and_counts_it_out(tmp_path: Path) -> None:
    """Armed Friday with a one-session grant; by Monday two sessions are spent."""
    artifacts_root, live_state_root = tmp_path / "clerk", tmp_path / "runner"
    _arm_on_disk(artifacts_root, live_state_root, max_sessions=1)

    observation = observe_arming(
        _shadow_runtime(),
        artifacts_root,
        lambda: live_state_root,
        # The environment must grant what the record sealed, or the instance
        # reports envelope disagreement before the lapse is ever counted.
        settings=live_settings(live_arming_max_sessions=1),
        now_ms=MONDAY_MS,
    )

    assert observation.armed_instance_count == 0
    assert observation.envelope_state == "sealed"
    # R11 requires the code; the phrase after it is what an operator reads in the tooltip.
    assert f"{ARMING_SID} (LIVE_ARMING_LAPSED: its sessions are spent)" in observation.detail


def test_observe_arming_fails_closed_on_an_unreadable_ledger(tmp_path: Path) -> None:
    artifacts_root, live_state_root = tmp_path / "clerk", tmp_path / "runner"
    _arm_on_disk(artifacts_root, live_state_root)
    ledger = LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT)
    ledger.path.write_text(
        ledger.path.read_text(encoding="utf-8").replace(
            f'"armed_at_ms":{ARMED_AT_MS}', f'"armed_at_ms":{ARMED_AT_MS + 1}'
        ),
        encoding="utf-8",
    )

    observation = observe_arming(
        _shadow_runtime(),
        artifacts_root,
        lambda: live_state_root,
        settings=live_settings(),
        now_ms=ARMED_AT_MS,
    )

    assert observation.armed_instance_count == 0
    assert observation.envelope_state == "configured_unsealed"
    # Not "the ledger cannot be read": the same branch also catches an
    # incomplete environment, and the exception is what names which.
    assert "The arming evidence cannot be judged" in observation.detail
    assert "digest does not verify" in observation.detail


def test_observe_arming_on_a_never_armed_account_claims_nothing(tmp_path: Path) -> None:
    """The shadow authority is installed and the ledger has no row at all."""
    artifacts_root, live_state_root = tmp_path / "clerk", tmp_path / "runner"

    observation = observe_arming(
        _shadow_runtime(),
        artifacts_root,
        lambda: live_state_root,
        settings=live_settings(),
        now_ms=ARMED_AT_MS,
    )

    assert observation.armed_instance_count == 0
    assert observation.envelope_state == "configured_unsealed"
    # No instance is named: there is no row to report as not-armed.
    assert observation.detail == ""


def test_observe_arming_never_resolves_the_bindings_root_it_will_not_read(tmp_path: Path) -> None:
    """The runner root is resolved from legacy IBKR settings, which can refuse.

    ``live_artifacts_root()`` constructs ``IbkrSettings``, so an invalid legacy
    IBKR environment raises. Nothing under that root can change a paper or
    absent-authority observation, and resolving it eagerly as a call argument
    turned the Alpaca live-verdict endpoint into a 500 for a perfectly valid
    paper configuration.
    """
    artifacts_root = tmp_path / "clerk"

    def _refuses() -> Path:
        pytest.fail("the bindings root was resolved for an observation that reads none")

    for runtime, settings in (
        (None, live_settings()),
        (ActiveClerkRuntime(authority_kind="sqlite", account_id="PA0SANITIZED00001"), live_settings()),
        (_shadow_runtime(), paper_settings()),
    ):
        assert (
            observe_arming(runtime, artifacts_root, _refuses, settings=settings, now_ms=ARMED_AT_MS)
            == ArmingObservation.none()
        )


def test_observe_arming_reads_nothing_off_a_paper_or_absent_authority(tmp_path: Path) -> None:
    artifacts_root, live_state_root = tmp_path / "clerk", tmp_path / "runner"
    _arm_on_disk(artifacts_root, live_state_root)

    for runtime, settings in (
        (None, live_settings()),
        (ActiveClerkRuntime(authority_kind="sqlite", account_id="PA0SANITIZED00001"), live_settings()),
        # The last of the four gates: the shadow authority *is* installed and
        # the ledger *is* armed, but the environment says paper -- so no arming
        # is claimed and no evidence is read.
        (_shadow_runtime(), paper_settings()),
    ):
        assert (
            observe_arming(
                runtime, artifacts_root, lambda: live_state_root, settings=settings, now_ms=ARMED_AT_MS
            )
            == ArmingObservation.none()
        )
