"""PRD Sec 11.5 legacy migration through the real BotResumeAdmission orchestration (#1728).

``test_signal_program_admission.py`` proves the reconstruction, append, and
clone-lineage primitives in isolation. This file proves ``BotResumeAdmission``
actually wires them into a live Resume: an exact-reconstructible instance
clears the ``PROGRAM_BUILD_UNPROVEN`` gate, an unprovable one is refused with
a typed escape hatch and mints its clone lineage exactly once even across two
Resume attempts.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.models import (
    AccountFreezeState,
    ClerkCustodySnapshot,
    CustodyCountFact,
    CustodyExposureFact,
    HoldState,
)
from app.schemas.broker_bots import BotStatusView
from app.schemas.market_liveness import MarketClockLivenessEvidence, MarketLivenessFact
from app.schemas.run_admission import (
    RunProcessAdmissionFact,
    StartRuntimeAdmissionFact,
    StrategyValidationAdmissionFact,
    TerminalEvidenceAdmissionFact,
)
from app.services.bot_binding_repository import BotBindingRepository, BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_resume_admission import BotResumeAdmission, LegacyMigrationLineageUnavailableError
from app.services.bot_start_admission import StartAdmissionDenied
from app.services.market_liveness import compose_market_liveness
from app.services.signal_program_admission import legacy_migration_clone_instance_id

_SID = "legacy-orchestration-1"
_NOW = 1_800_000_000_000


def _count(state: str = "zero") -> CustodyCountFact:
    return CustodyCountFact(state=state, count=0 if state == "zero" else None)


def _clerk(observed_at_ms: int) -> ClerkCustodySnapshot:
    return ClerkCustodySnapshot(
        broker="alpaca",
        account_id=f"sim:{_SID}",
        strategy_instance_id=_SID,
        clerk_generation="clerk-1",
        journal_sequence=1,
        reconciliation_state="clean",
        reconciliation_fresh=True,
        reconciled_at_ms=observed_at_ms,
        exposure=CustodyExposureFact(state="zero", positions={}),
        working_orders=_count(),
        pending_orders=_count(),
        terminal_orders=_count(),
        unresolved_effects=_count(),
        hold=HoldState(active=False),
        freeze=AccountFreezeState(),
        reason_code="CLERK_CUSTODY_PROVEN",
        evidence_refs=(f"clerk:sim:{_SID}:1",),
        observed_at_ms=observed_at_ms,
    )


def _prior(*, strategy_params: dict[str, object] | None) -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=_SID,
        strategy_key="ema_crossover_signal",
        broker="alpaca",
        symbol="SPY",
        use_rth=True,
        mode="dry_run",
        quantity=1,
        carryover_policy="FORBID",
        action_plan=alpaca_v1_action_plan("SPY"),
        strategy_params=strategy_params,
        sealed_account_id=f"sim:{_SID}",
        run_id="run-prior",
        created_at_ms=_NOW,
    )


def _status() -> BotStatusView:
    return BotStatusView(
        strategy_instance_id=_SID,
        broker="alpaca",
        symbol="SPY",
        mode="dry_run",
        quantity=1,
        running=False,
        phase="OFF_DUTY",
        desired_state="STOPPED",
        active_run_id=None,
        duty_outcome=None,
        binding_created_at_ms=_NOW,
        last_transition_at_ms=None,
    )


def _admission(repository: BotBindingRepository | None, *, now_values: list[int]) -> BotResumeAdmission:
    clock = iter(now_values)

    def now_ms() -> int:
        return next(clock)

    async def runtime_fact(strategy_instance_id: str, observed_at_ms: int) -> StartRuntimeAdmissionFact:
        del strategy_instance_id
        return StartRuntimeAdmissionFact(state="READY", observed_at_ms=observed_at_ms, explanation="ready")

    def process_fact(binding: object, observed_at_ms: int) -> RunProcessAdmissionFact:
        del binding
        return RunProcessAdmissionFact(
            state="EXITED",
            run_id="run-prior",
            process_identity="proc-1",
            registry_generation="registry-1",
            observed_at_ms=observed_at_ms,
        )

    @asynccontextmanager
    async def custody_guard(binding: BrokerBotBinding):
        yield _clerk(observed_at_ms=binding.created_at_ms)

    def validation_fact(_binding: object, observed_at_ms: int) -> StrategyValidationAdmissionFact:
        return StrategyValidationAdmissionFact(
            state="VERIFIED",
            strategy_key="ema_crossover_signal",
            evidence_status="accepted",
            event_id="validation-legacy-1",
            evidence_snapshot_sha256="d" * 64,
            verified_at_ms=observed_at_ms,
            explanation="The validation proof is current.",
        )

    def market_liveness(symbol: str, observed_at_ms: int) -> MarketLivenessFact:
        return compose_market_liveness(
            symbol,
            now_ms=observed_at_ms,
            market_clock=MarketClockLivenessEvidence(
                state="OPEN",
                source="test.clock",
                observed_at_ms=observed_at_ms,
                vendor_timestamp_ms=observed_at_ms,
            ),
            connected=True,
            connection_changed_at_ms=observed_at_ms,
            symbol_status=None,
        )

    async def activate(*args: object, **kwargs: object) -> None:
        raise AssertionError("activate must not be called when admission is refused")

    return BotResumeAdmission(
        now_ms=now_ms,
        feed_resolver=lambda: None,
        custody_guard=custody_guard,
        process_fact=process_fact,
        runtime_fact=runtime_fact,
        checkpoint=lambda binding: None,
        terminal_evidence=lambda binding: TerminalEvidenceAdmissionFact(
            state="SUMMARY_READY",
            evidence_ref="terminal-evidence:run-prior:absent",
            explanation="No terminal evidence blocks Resume for the prior run.",
        ),
        validation_fact=validation_fact,
        activate=activate,
        carryover_account_policy_enabled=False,
        session_capability=lambda symbol, account_id: None,
        market_liveness=market_liveness,
        legacy_migration_repository=repository,
    )


@pytest.mark.asyncio
async def test_resume_preview_appends_a_reconstructible_seal_and_clears_program_build(
) -> None:
    """PRD Sec 11.5 append case: preview alone proves reconstruction, no clone.

    ``strategy_params=None`` models a legacy instance that never supplied
    explicit params -- ``_prior`` never sets ``strategy_param_origins``
    either, matching a real Resume's disk-read binding exactly (that field
    is ``exclude=True`` and never round-trips). Every parameter is
    therefore factually absent ("registered_default"), so reconstruction is
    exact with no origin recorded and no clone. A binding whose
    ``strategy_params`` instead recorded explicit values with no origin
    evidence has no factual source for them and must clone -- see
    ``test_resume_fails_closed_when_no_lineage_writer_is_configured`` below.
    """
    prior = _prior(strategy_params=None)
    admission = _admission(repository=None, now_values=[_NOW, _NOW + 1, _NOW + 2, _NOW + 3])

    decision = await admission.preview(prior, _status())

    assert decision.reason_code != "PROGRAM_BUILD_UNPROVEN"


@pytest.mark.asyncio
async def test_resume_refuses_and_clones_lineage_exactly_once_across_two_attempts(
    tmp_path: Path,
) -> None:
    """PRD Sec 11.5 clone case: two Resume attempts, exactly one durable clone."""
    repository = BotBindingRepository(
        tmp_path,
        instance_dir_for=lambda strategy_instance_id: tmp_path / "live_state" / strategy_instance_id,
    )
    prior = _prior(strategy_params={"gap": -1.0, "rsi_min": 50.0, "rsi_max": 70.0})
    clone_id = legacy_migration_clone_instance_id(prior.strategy_instance_id)

    admission_first = _admission(repository=repository, now_values=[_NOW, _NOW + 1, _NOW + 2, _NOW + 3])
    with pytest.raises(StartAdmissionDenied) as first_exc:
        await admission_first.resume(prior, _status())

    admission_second = _admission(
        repository=repository,
        now_values=[_NOW + 100, _NOW + 101, _NOW + 102, _NOW + 103],
    )
    with pytest.raises(StartAdmissionDenied) as second_exc:
        await admission_second.resume(prior, _status())

    assert first_exc.value.decision.reason_code == "PROGRAM_BUILD_UNPROVEN"
    assert second_exc.value.decision.reason_code == "PROGRAM_BUILD_UNPROVEN"
    assert clone_id in first_exc.value.decision.next_step
    assert clone_id in second_exc.value.decision.next_step

    lineage = repository.read_legacy_migration_lineage(clone_id)
    assert lineage is not None
    assert lineage.migrated_from_strategy_instance_id == prior.strategy_instance_id
    # The second (later, different-timestamp) attempt did not mint a second
    # clone or overwrite the first one's evidence.
    assert lineage.created_at_ms == _NOW + 2

    # preview() never persists — a poll before either resume() call would
    # have left nothing on disk.
    admission_preview = _admission(repository=repository, now_values=[_NOW, _NOW + 1, _NOW + 2, _NOW + 3])
    other_prior = _prior(strategy_params={"gap": -2.0, "rsi_min": 50.0, "rsi_max": 70.0}).model_copy(
        update={"strategy_instance_id": "legacy-orchestration-preview-only"}
    )
    other_clone_id = legacy_migration_clone_instance_id(other_prior.strategy_instance_id)
    await admission_preview.preview(other_prior, _status())
    assert repository.read_legacy_migration_lineage(other_clone_id) is None


@pytest.mark.asyncio
async def test_resume_fails_closed_when_no_lineage_writer_is_configured() -> None:
    """Independent-review Defect 2: a mutating Resume must not promise lineage it never wrote.

    ``_resolve_program_build``'s clone branch returns a decision whose
    ``next_step`` tells the operator the clone "carries explicit lineage
    back to this instance" -- a durable-evidence claim, not an aspiration.
    Without a configured lineage writer, ``resume()`` (the mutating call)
    must fail loudly instead of completing with that false promise.
    ``preview()`` is unaffected: it never persists lineage by design.
    """
    prior = _prior(strategy_params={"gap": -1.0, "rsi_min": 50.0, "rsi_max": 70.0})
    admission = _admission(repository=None, now_values=[_NOW, _NOW + 1, _NOW + 2, _NOW + 3])

    with pytest.raises(LegacyMigrationLineageUnavailableError):
        await admission.resume(prior, _status())
