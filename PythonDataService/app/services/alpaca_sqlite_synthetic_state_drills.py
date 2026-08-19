"""Authority-scope and restart scenarios for the synthetic SQLite rehearsal."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.broker.alpaca.clerk.active_authority import select_active_clerk_runtime
from app.broker.alpaca.clerk.sqlite.activation import ActivationRecord
from app.broker.alpaca.clerk.sqlite.enter import accept_enter, submit_enter
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    ORDER_OUTCOME_UNKNOWN_REASON_CODE,
    admit_new_exposure,
    raise_uncertainty,
)
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerAccountSnapshot
from app.services.account_custody_synthetic_scenarios import SyntheticScenarioId
from app.services.alpaca_sqlite_synthetic_drill_support import (
    SyntheticBroker,
    SyntheticScenarioObservation,
    SyntheticScenarioStatus,
    TickClock,
    new_repo,
    synthetic_leg,
    worksheet,
)
from app.services.alpaca_sqlite_synthetic_drill_support import (
    require_invariant as _require,
)


class _StartupSyntheticBroker(SyntheticBroker):
    """Deterministic paper account plus broker truth for the real boot selector."""

    def __init__(self, clock: TickClock, *, account_id: str) -> None:
        super().__init__(clock)
        self._account_id = account_id

    async def get_account(self) -> BrokerAccountSnapshot:
        now = self.clock()
        return BrokerAccountSnapshot(
            broker="alpaca",
            account_id=self._account_id,
            account_mode="paper",
            account_status="ACTIVE",
            currency="USD",
            cash=100_000.0,
            equity=100_000.0,
            buying_power=200_000.0,
            portfolio_value=100_000.0,
            long_market_value=0.0,
            short_market_value=0.0,
            pattern_day_trader=False,
            trading_blocked=False,
            account_blocked=False,
            created_at_ms=now,
            observed_at_ms=now,
        )


@dataclass(frozen=True)
class _SyntheticActivationResolver:
    record: ActivationRecord

    def latest(self, account_id: str) -> ActivationRecord | None:
        return self.record if account_id == self.record.account_id else None

    def resolve(
        self,
        account_id: str,
        authority_generation: int,
        db_identity_token: str,
        artifacts_root: Path,
    ) -> ActivationRecord | None:
        del artifacts_root
        if (
            account_id != self.record.account_id
            or authority_generation != self.record.authority_generation
            or db_identity_token != self.record.db_identity_token
        ):
            return None
        return self.record


async def bot_isolation(artifacts_root: Path) -> SyntheticScenarioObservation:
    repo, clock, _account_id, _runs = new_repo(
        artifacts_root,
        suffix="bot-isolation",
        bot_ids=("spy-bot-a", "spy-bot-b"),
    )
    broker = SyntheticBroker(clock)
    try:
        revision = repo.control_meta_snapshot().control_revision
        before = broker.proof()
        uncertainty_source_event_at_ms = clock()
        raise_uncertainty(
            repo,
            strategy_instance_id="spy-bot-a",
            reason_code=ORDER_OUTCOME_UNKNOWN_REASON_CODE,
            headline="Synthetic order uncertainty",
            explanation="Only bot A owns this order identity.",
            operator_impact="Bot A cannot add exposure.",
            next_step="Resolve exact order identity.",
            cause_facts={"order_ref": "synthetic-order"},
        )
        a = admit_new_exposure(repo, strategy_instance_id="spy-bot-a")
        b = admit_new_exposure(repo, strategy_instance_id="spy-bot-b")
        passed = not a.allowed and b.allowed
        return SyntheticScenarioObservation(
            scenario_id=SyntheticScenarioId.BOT_UNCERTAINTY_ISOLATION,
            status=SyntheticScenarioStatus.PASSED if passed else SyntheticScenarioStatus.FAILED,
            assertion="A recognized BOT uncertainty blocked only its owning bot.",
            evidence=worksheet(
                repo,
                revision_before=revision,
                broker_before=before,
                broker_after=broker.proof(),
                expected="bot A blocked; bot B admitted",
                observed=f"bot_a={a.allowed}; bot_b={b.allowed}; reason={a.reason_code}",
                action="ADMISSION_CHECK",
                source_event_at_ms=uncertainty_source_event_at_ms,
            ),
        )
    finally:
        repo.close()


async def unknown_account_scope(artifacts_root: Path) -> SyntheticScenarioObservation:
    repo, clock, _account_id, _runs = new_repo(
        artifacts_root,
        suffix="unknown",
        bot_ids=("spy-bot-a", "spy-bot-b"),
    )
    broker = SyntheticBroker(clock)
    try:
        revision = repo.control_meta_snapshot().control_revision
        before = broker.proof()
        uncertainty_source_event_at_ms = clock()
        raise_uncertainty(
            repo,
            strategy_instance_id="spy-bot-a",
            reason_code="SYNTHETIC_UNCLASSIFIED_CAUSE",
            headline="Unknown synthetic uncertainty",
            explanation="Unclassified causes must fail closed.",
            operator_impact="Account exposure is blocked.",
            next_step="Classify the cause.",
            cause_facts={"raw": "unclassified"},
        )
        a = admit_new_exposure(repo, strategy_instance_id="spy-bot-a")
        b = admit_new_exposure(repo, strategy_instance_id="spy-bot-b")
        active = repo.active_uncertainty(
            scope="ACCOUNT_CLERK",
            reason_code="SYNTHETIC_UNCLASSIFIED_CAUSE",
            strategy_instance_id=None,
        )
        passed = active is not None and not a.allowed and not b.allowed
        return SyntheticScenarioObservation(
            scenario_id=SyntheticScenarioId.UNCLASSIFIED_UNCERTAINTY_ACCOUNT_CLERK,
            status=SyntheticScenarioStatus.PASSED if passed else SyntheticScenarioStatus.FAILED,
            assertion="An unknown cause widened to ACCOUNT_CLERK and blocked every bot.",
            evidence=worksheet(
                repo,
                revision_before=revision,
                broker_before=before,
                broker_after=broker.proof(),
                expected="ACCOUNT_CLERK scope; both bots blocked",
                observed=(f"scope={active['scope'] if active else None}; bot_a={a.allowed}; bot_b={b.allowed}"),
                action="ADMISSION_CHECK",
                source_event_at_ms=uncertainty_source_event_at_ms,
            ),
        )
    finally:
        repo.close()


async def restart_in_flight(artifacts_root: Path) -> SyntheticScenarioObservation:
    repo, clock, account_id, runs = new_repo(
        artifacts_root,
        suffix="restart",
        bot_ids=("accepted-bot", "unknown-bot"),
    )
    broker = _StartupSyntheticBroker(clock, account_id=account_id)
    try:
        accepted = accept_enter(
            repo,
            account_id=account_id,
            strategy_instance_id="accepted-bot",
            decision_id="restart-accepted",
            lifecycle_run_id=runs["accepted-bot"],
            leg=synthetic_leg(),
        )
        _require(accepted.order_ref is not None, "restart accepted work must mint an order_ref")
        broker.seed_order(accepted.order_ref)

        broker.submit_error = BrokerUnavailable(
            "synthetic submit response lost before restart",
            broker="alpaca",
        )
        unknown = await submit_enter(
            repo,
            account_id=account_id,
            strategy_instance_id="unknown-bot",
            decision_id="restart-unknown",
            lifecycle_run_id=runs["unknown-bot"],
            leg=synthetic_leg(),
            trade=broker,
        )
        _require(unknown.order_ref is not None, "restart unknown work must mint an order_ref")
        unknown_before = repo.effect_operation(unknown.effect_operation_id)
        _require(
            unknown_before is not None and unknown_before.state == "unknown",
            "restart drill must persist unknown work before recovery",
        )
        broker.submit_error = None
        broker.seed_order(unknown.order_ref)

        revision = repo.control_meta_snapshot().control_revision
        before = broker.proof()
        meta_before = repo.control_meta_snapshot()
        activation = ActivationRecord.create(
            account_id=account_id,
            authority_generation=meta_before.authority_generation,
            db_identity_token=meta_before.db_identity_token,
            broker_proof_reference="synthetic-broker-proof.json",
            broker_proof_sha256="0" * 64,
            legacy_quarantine_manifest="synthetic-quarantine.json",
            legacy_quarantine_manifest_sha256="1" * 64,
            activated_at_ms=clock(),
        )
        restart_source_event_at_ms = clock()
    finally:
        repo.close()

    runtime = await select_active_clerk_runtime(
        read=broker,
        trade=broker,
        artifacts_root=artifacts_root,
        activation_store=_SyntheticActivationResolver(activation),
        repository_opener=lambda opened_account_id, opened_root: ClerkSqliteRepository.open(
            account_id=opened_account_id,
            artifacts_root=opened_root,
            clock=clock,
            lease_ttl_ms=300_000,
        ),
    )
    try:
        recovered = runtime.sqlite_repository
        _require(
            runtime.authority_kind == "sqlite" and recovered is not None,
            "restart drill must recover the SQLite authority",
        )
        meta_after = recovered.control_meta_snapshot()
        accepted_order = recovered.order(accepted.order_ref)
        unknown_order = recovered.order(unknown.order_ref)
        accepted_effect = recovered.effect_operation(accepted.effect_operation_id)
        unknown_effect = recovered.effect_operation(unknown.effect_operation_id)
        unknown_hold = recovered.active_uncertainty(
            scope="CUSTODY_SUBJECT",
            reason_code=ORDER_OUTCOME_UNKNOWN_REASON_CODE,
            strategy_instance_id="unknown-bot",
        )
        boot_submit_count = len(broker.submit_calls) - len(before.submit_calls)
        boot_lookups = tuple(broker.lookup_calls[len(before.lookup_calls) :])
        passed = (
            meta_after.db_identity_token == meta_before.db_identity_token
            and accepted_order is not None
            and accepted_order.broker_order_id is not None
            and unknown_order is not None
            and unknown_order.broker_order_id is not None
            and accepted_effect is not None
            and accepted_effect.state == "in_progress"
            and unknown_effect is not None
            and unknown_effect.state == "in_progress"
            and unknown_hold is None
            and boot_submit_count == 0
            and boot_lookups == (accepted.order_ref, unknown.order_ref)
        )
        return SyntheticScenarioObservation(
            scenario_id=SyntheticScenarioId.RESTART_IN_FLIGHT_WORK,
            status=SyntheticScenarioStatus.PASSED if passed else SyntheticScenarioStatus.FAILED,
            assertion=(
                "The active-authority boot path reopened the same SQLite generation and "
                "recovered accepted and unknown work by exact identity without resubmission."
            ),
            evidence=worksheet(
                recovered,
                revision_before=revision,
                broker_before=before,
                broker_after=broker.proof(),
                expected=(
                    "same db identity; accepted and unknown work resolve during service boot; "
                    "two exact lookups; zero boot submit calls"
                ),
                observed=(
                    f"same_identity={meta_after.db_identity_token == meta_before.db_identity_token}; "
                    f"accepted_state={accepted_effect.state if accepted_effect else None}; "
                    f"unknown_state={unknown_effect.state if unknown_effect else None}; "
                    f"unknown_hold={unknown_hold is not None}; "
                    f"boot_lookups={boot_lookups}; boot_submit_count={boot_submit_count}"
                ),
                action="RESTART_AND_RECOVER",
                source_event_at_ms=restart_source_event_at_ms,
            ),
        )
    finally:
        await runtime.close()


__all__ = ["bot_isolation", "restart_in_flight", "unknown_account_scope"]
