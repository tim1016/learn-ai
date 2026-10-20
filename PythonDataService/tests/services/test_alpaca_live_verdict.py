"""The Alpaca live verdict is a pure function of settings and clerk selection
(ADR 0059 D8). It never contacts the broker and never guesses."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    ClerkStartupFailure,
)
from app.broker.alpaca.config import AlpacaSettings
from app.services.alpaca_live_verdict import (
    alpaca_live_verdict,
    observe_loss_hold,
)
from tests.broker.alpaca.clerk.live_envelope_fixtures import (
    LIVE_ACCT,
    _LiveBroker,
)
from tests.broker.alpaca.clerk.test_shadow_envelope_runtime import shadow_runtime as shadow_runtime

_NOW = 1_800_000_000_000
_LIVE = {
    "live_loss_fraction": 0.02, "live_loss_usd": 500.0, "live_xh_entry_bps": 10.0, "live_xh_exit_bps": 10.0,
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


@pytest.mark.parametrize(
    ("make_settings", "configured_mode", "final_verdict"),
    [
        pytest.param(lambda: None, "unconfigured", "unknown", id="unconfigured-settings-is-unknown"),
        pytest.param(_paper, "paper", "paper", id="paper-settings-is-paper-regardless-of-clerk-state"),
        pytest.param(_live, "live", "unknown", id="live-with-no-runtime-yet-is-unknown"),
    ],
)
def test_a_verdict_with_no_runtime_installed_reads_only_the_settings(
    make_settings: Callable[[], AlpacaSettings | None], configured_mode: str, final_verdict: str
) -> None:
    verdict = alpaca_live_verdict(settings=make_settings(), runtime=None, now_ms=_NOW)

    assert verdict.configured_mode == configured_mode, "configured mode is read from the settings"
    assert verdict.final_verdict == final_verdict, "final verdict follows the settings alone"
    assert verdict.observed_at_ms == _NOW, "observed_at_ms is the caller's clock"
    assert verdict.clerk_authority == "not_installed", "no runtime means no clerk authority is installed"
    assert verdict.deployment_readiness == "not_applicable", "no runtime means no deployment readiness"


def test_live_with_refused_clerk_is_live_unarmed_and_names_the_account() -> None:
    runtime = _failure("LIVE_ACCOUNT_REFUSED", "9LIVE0001")

    verdict = alpaca_live_verdict(settings=_live(), runtime=runtime, now_ms=_NOW)

    assert verdict.configured_mode == "live"
    assert verdict.observed_account_id == "9LIVE0001"
    assert verdict.mode_agreement == "agreed"
    assert verdict.clerk_refusal_reason_code == "LIVE_ACCOUNT_REFUSED"
    assert verdict.final_verdict == "live"
    assert "9LIVE0001" in verdict.headline


@pytest.mark.parametrize(
    ("make_settings", "configured_mode", "reason_code", "mode_agreement"),
    [
        pytest.param(
            _live, "live", "LIVE_MODE_DISAGREEMENT", "disagreed",
            id="live-with-mode-disagreement-is-unknown-and-disagreed",
        ),
        pytest.param(
            _live, "live", "BROKER_ACCOUNT_UNAVAILABLE", "unobserved",
            id="live-with-no-account-observed-is-unknown-and-unobserved",
        ),
        pytest.param(
            _paper, "paper", "LIVE_MODE_DISAGREEMENT", "disagreed",
            id="paper-with-mode-disagreement-is-unknown-not-reassuring",
        ),
    ],
)
def test_an_unverified_account_mode_is_unknown_and_never_reassuring(
    make_settings: Callable[[], AlpacaSettings], configured_mode: str, reason_code: str, mode_agreement: str
) -> None:
    runtime = _failure(reason_code, None)

    verdict = alpaca_live_verdict(settings=make_settings(), runtime=runtime, now_ms=_NOW)

    assert verdict.configured_mode == configured_mode, "configured mode is read from the settings"
    assert verdict.mode_agreement == mode_agreement, "mode agreement follows the clerk's refusal"
    assert verdict.final_verdict == "unknown", "an unverified account mode is unknown"
    assert "no real money" not in verdict.headline, "an unknown verdict never reassures"
    assert verdict.clerk_refusal_reason_code == reason_code, "the clerk's refusal reason is carried"


def test_a_mid_session_mode_disagreement_the_envelope_sync_observed_is_disagreed() -> None:
    """#2629: the envelope sync is the one observer; no arming gate echoes it any more."""
    runtime = ActiveClerkRuntime(
        authority_kind="sqlite",
        account_id=LIVE_ACCT,
        envelope_sync=SimpleNamespace(account_mode_disagreed=True),  # type: ignore[arg-type]
    )

    verdict = alpaca_live_verdict(settings=_live(), runtime=runtime, now_ms=_NOW)

    assert verdict.mode_agreement == "disagreed"
    assert verdict.clerk_refusal_reason_code == "LIVE_MODE_DISAGREEMENT"
    assert verdict.final_verdict == "unknown"


def test_clean_paper_selection_is_the_normal_path() -> None:
    runtime = ActiveClerkRuntime(authority_kind="sqlite", account_id="PA0SANITIZED00001")

    verdict = alpaca_live_verdict(settings=_paper(), runtime=runtime, now_ms=_NOW)

    assert verdict.clerk_authority == "sqlite"
    assert verdict.clerk_refusal_reason_code is None
    assert verdict.mode_agreement == "agreed"
    assert verdict.final_verdict == "paper"
    assert "Paper account" in verdict.headline



async def test_shadow_verdict_reports_budget_readiness_without_reading_retired_records(shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker], tmp_path: Path) -> None:
    from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
    from app.broker.alpaca.clerk.sqlite.budget_authority import commit_budget_authority_cutover

    runtime, _ = shadow_runtime
    retired = LiveArmingLedger(tmp_path, live_account_id=LIVE_ACCT).path
    retired.parent.mkdir(parents=True, exist_ok=True)
    retired.write_text("corrupt old permission\n")
    first = alpaca_live_verdict(settings=_live(), runtime=runtime, now_ms=_NOW)
    assert first.final_verdict == "shadow"
    assert first.deployment_readiness == "upgrade_required"
    assert first.budget_authority_version == 1
    repo = runtime.sqlite_repository
    commit_budget_authority_cutover(repo, actor="test", reviewed_token="reviewed", stop_receipt="stopped")
    await runtime.envelope_sync.tick()
    current = alpaca_live_verdict(settings=_live(), runtime=runtime, now_ms=_NOW)
    assert current.deployment_readiness == "ready"
    assert current.budget_authority_version == 2
    assert "own reviewed budget" in current.detail
    assert not {"armed_instance_count", "envelope_state", "shadow_state", "envelope_agreement"}.intersection(current.model_dump())
    assert retired.read_text() == "corrupt old permission\n"
    runtime.envelope_sync.discard_observation()
    unknown = alpaca_live_verdict(settings=_live(), runtime=runtime, now_ms=_NOW)
    assert unknown.deployment_readiness == "risk_not_observed"


async def test_standing_hold_overrides_fresh_risk_in_account_verdict(shadow_runtime: tuple[ActiveClerkRuntime, _LiveBroker]) -> None:
    from app.broker.alpaca.clerk.sqlite.budget_authority import commit_budget_authority_cutover
    from app.broker.alpaca.clerk.sqlite.uncertainty_causes import LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE

    runtime, _ = shadow_runtime
    repo = runtime.sqlite_repository
    commit_budget_authority_cutover(repo, actor="test", reviewed_token="reviewed", stop_receipt="stopped")
    from app.broker.alpaca.clerk.sqlite.uncertainty import raise_uncertainty
    from app.broker.alpaca.clerk.sqlite.uncertainty_causes import LossHoldCause
    raise_uncertainty(repo, reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, strategy_instance_id=None, headline="Loss hold", explanation="Prior breach", operator_impact="Entries held", next_step="Review", cause_facts=LossHoldCause(day_start_ms=repo.clock() - 1000, day_pnl_usd=-5000., loss_limit_usd=5000., last_equity_usd=100000., observed_at_ms=repo.clock()).to_mapping())
    assert observe_loss_hold(runtime) == "held"
    verdict = alpaca_live_verdict(settings=_live(), runtime=runtime, now_ms=_NOW)
    assert verdict.deployment_readiness == "loss_hold"
    assert "reducing exits" in verdict.detail


async def test_pill_tooltip_names_a_missing_loss_limit_and_its_fix_in_settings(tmp_path: Path) -> None:
    """H7: an account with no daily loss limit refuses every new entry, and
    the pill's tooltip said only "Current risk evidence is unavailable". It
    now says what the shared risk check says -- the limit is missing and is
    set in Settings -- the same words Deploy and Settings show."""
    from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate
    from app.broker.alpaca.clerk.sqlite.budget_authority import commit_budget_authority_cutover
    from app.broker.alpaca.clerk.sqlite.live_envelope_sync import LiveEnvelopeSync
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
    from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock, complete_fee_evidence
    from tests.broker.alpaca.clerk.sqlite.test_live_envelope_sync import _Read

    repo = ClerkSqliteRepository.initialize(account_id="PA-PILL", artifacts_root=tmp_path, clock=_TestClock(NOON))
    complete_fee_evidence(repo)
    commit_budget_authority_cutover(repo, actor="owner", reviewed_token="reviewed", stop_receipt="stopped")
    sync = LiveEnvelopeSync(repo=repo, read=_Read(unrealized=0), envelope=LiveEnvelopeGate(values=None, custody_is_simulated=False))
    runtime = ActiveClerkRuntime(authority_kind="sqlite", envelope_sync=sync, _sqlite_repository=repo, account_id=repo.account_id)
    try:
        verdict = alpaca_live_verdict(settings=_paper(), runtime=runtime, now_ms=NOON)

        assert verdict.final_verdict == "paper"
        assert verdict.deployment_readiness == "risk_not_observed"
        assert verdict.detail == (
            "Orders reach Alpaca's paper endpoint only. "
            "No daily loss limit is set for this account, so new entries are refused. Set one in Settings."
        )
    finally:
        await runtime.close()
