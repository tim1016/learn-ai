"""Server-owned runtime observations for one receipt-pinned cohort."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from app.engine.live.account_artifacts import CohortBatchLaunchMemberPin, CohortBatchLaunchReceipt
from app.engine.live.engine_runtime import ENGINE_RUNTIME_FILENAME, read_engine_runtime_snapshot
from app.engine.live.readiness_sidecar import read_readiness
from app.schemas.live_runs import ReadinessVector
from app.services.account_truth_snapshot import (
    AccountTruthUnavailable,
    assess_account_truth,
    get_account_truth_snapshot_provider,
)
from app.services.cohort_evidence import CohortEvidenceSample, CohortMemberSample
from app.services.fleet_contamination import compute_account_fleet_contamination, read_instance_live_state
from app.services.runtime_freshness import evaluate_runtime_freshness

VisibleRuns = Callable[[Path], dict[str, list[dict[str, object]]]]
NowMs = Callable[[], int]


class CohortEvidenceRuntimeObserver:
    """Reads canonical server inputs without letting a browser define proof."""

    def __init__(self, *, live_runs_root: Path, visible_runs_by_instance: VisibleRuns, now_ms: NowMs) -> None:
        self._live_runs_root = live_runs_root
        self._visible_runs_by_instance = visible_runs_by_instance
        self._now_ms = now_ms

    async def observe(
        self,
        receipt: CohortBatchLaunchReceipt,
        expected_at_ms: int,
    ) -> CohortEvidenceSample:
        """Return one exact-tick observation from account and engine authorities."""

        truth_evidence = get_account_truth_snapshot_provider().get(receipt.account_id)
        truth = assess_account_truth(truth_evidence, now_ms=expected_at_ms)
        truth_state = (
            "unknown"
            if truth_evidence is None or isinstance(truth_evidence, AccountTruthUnavailable)
            else "healthy"
            if truth.status == "pass"
            else "failed"
        )
        try:
            fleet = await compute_account_fleet_contamination(
                self._live_runs_root,
                account_id=receipt.account_id,
            )
            fleet_state = (
                "healthy"
                if fleet.verdict == "clean"
                else "unknown"
                if fleet.verdict == "unknown"
                else "failed"
            )
            broker_net_positions = fleet.net_positions
            broker_residual = fleet.residual
        except Exception:
            fleet_state = "unknown"
            broker_net_positions = None
            broker_residual = None
        runs = await asyncio.to_thread(self._visible_runs_by_instance, self._live_runs_root)
        return CohortEvidenceSample(
            expected_at_ms=expected_at_ms,
            observed_at_ms=self._now_ms(),
            account_truth=truth_state,
            fleet=fleet_state,
            members=tuple(self._member(pin, runs) for pin in receipt.member_pins),
            broker_net_positions=broker_net_positions,
            broker_residual=broker_residual,
        )

    def _member(
        self,
        pin: CohortBatchLaunchMemberPin,
        runs: dict[str, list[dict[str, object]]],
    ) -> CohortMemberSample:
        run = next(
            (item for item in runs.get(pin.strategy_instance_id, []) if item.get("run_id") == pin.run_id),
            None,
        )
        if run is None:
            return CohortMemberSample(pin.strategy_instance_id, pin.run_id, "failed", "COHORT_PIN_MISSING")
        envelope = read_instance_live_state(self._live_runs_root, pin.strategy_instance_id)
        if envelope is None:
            return CohortMemberSample(pin.strategy_instance_id, pin.run_id, "unknown", "RUNTIME_STATE_MISSING")
        if envelope.run_id != pin.run_id:
            return CohortMemberSample(pin.strategy_instance_id, envelope.run_id, "failed", "COHORT_RUN_RESTARTED")
        run_dir = Path(str(run["run_dir"]))
        if (
            envelope.poisoned_reason is not None
            or (run_dir / "halt.flag").exists()
            or (run_dir / "poisoned.flag").exists()
        ):
            return CohortMemberSample(pin.strategy_instance_id, pin.run_id, "failed", "COHORT_MEMBER_HALTED")
        runtime = read_engine_runtime_snapshot(run_dir / ENGINE_RUNTIME_FILENAME)
        if runtime is None or runtime.run_id != pin.run_id:
            return CohortMemberSample(pin.strategy_instance_id, pin.run_id, "unknown", "RUNTIME_PROOF_MISSING")
        freshness = evaluate_runtime_freshness(runtime, now_ms=self._now_ms())
        if freshness.posture_demoted or runtime.command_loop.state != "RUNNING":
            return CohortMemberSample(pin.strategy_instance_id, pin.run_id, "unknown", "RUNTIME_NOT_LIVE_OR_FRESH")
        readiness_raw = read_readiness(run_dir)
        if readiness_raw is None:
            return CohortMemberSample(pin.strategy_instance_id, pin.run_id, "unknown", "RUNTIME_COUNTERS_MISSING")
        try:
            readiness = ReadinessVector.model_validate(readiness_raw)
        except ValueError:
            return CohortMemberSample(pin.strategy_instance_id, pin.run_id, "unknown", "RUNTIME_COUNTERS_UNREADABLE")
        if readiness.verdict != "READY" or any(gate.status != "pass" for gate in readiness.gates):
            return CohortMemberSample(pin.strategy_instance_id, pin.run_id, "failed", "RUNTIME_READINESS_BLOCKED")
        if readiness.orders_used is None or readiness.orders_cap is None:
            return CohortMemberSample(pin.strategy_instance_id, pin.run_id, "unknown", "RUNTIME_COUNTERS_MISSING")
        return CohortMemberSample(
            pin.strategy_instance_id,
            pin.run_id,
            "healthy",
            orders_used=readiness.orders_used,
            orders_cap=readiness.orders_cap,
        )
