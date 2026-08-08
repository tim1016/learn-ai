"""Bounded tests for the SQLite Clerk qualification evidence runner."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.alpaca.clerk.sqlite import qualification
from app.broker.alpaca.clerk.sqlite import qualification_storage_recovery as storage_recovery
from app.broker.alpaca.clerk.sqlite.qualification import (
    PROFILE_SCALES,
    PolygonMinuteAggregate,
    QualificationStorageBoundary,
    SyntheticRecoveryCustodyError,
    SyntheticRehearsalPhaseError,
    SyntheticTestOutcome,
    qualification_markdown,
    run_performance_profile,
    run_polygon_replay,
    run_synthetic_custody_rehearsal,
    synthesize_polygon_5s_bars,
)
from app.broker.alpaca.clerk.sqlite.qualification_storage_recovery import (
    prove_mount_topology,
)
from app.broker.alpaca.clerk.sqlite.qualification_synthetic_rehearsal import SyntheticTestFailureOrigin
from app.broker.alpaca.clerk.sqlite.recovery import (
    RecoveryRefusalReason,
    RecoveryRefused,
)
from app.schemas.account_custody_qualification import SyntheticUiCorrelationEvidence
from app.services.account_custody_synthetic_scenarios import (
    SYNTHETIC_REHEARSAL_SCENARIOS,
    SyntheticScenarioCategory,
    SyntheticScenarioId,
)

_POLYGON_FIXTURE = Path(__file__).parents[4] / "fixtures" / "polygon_capture" / "spy_minute_2025-01-13_2025-01-17"


def _ui_correlation_records() -> tuple[dict[str, object], ...]:
    revisions = (41, 41, 42, 42, 43)
    return tuple(
        {
            "page_load_index": page_load_index,
            "event_index": event_index,
            "browser_epoch": f"browser-reload-{page_load_index}",
            "revision": revision,
            "historical_snapshot_state": "OFF_DUTY_STOPPED_FLAT_POST_RESTART",
            "presented_action_id": "resume",
            "click_target": "BROKER_ACK_RAW_EVIDENCE",
            "evidence_request_method": "GET",
            "evidence_request_path": (
                "/api/brokers/alpaca/accounts/DUM284968/bots/sid-001/evidence"
                "?transaction_ref=tx-001&client_hint=operator-transaction-timeline"
            ),
            "operation_reference": f"receipt-{page_load_index}-{event_index}",
            "proof_reference": f"receipt-{page_load_index}-{event_index}",
            "dispatched_lifecycle_action_id": None,
            "durable_lifecycle_receipt_id": None,
        }
        for page_load_index in range(5)
        for event_index, revision in enumerate(revisions)
    )


def test_full_profile_pins_required_scale_points() -> None:
    assert PROFILE_SCALES["full"] == (
        (1, 10_000),
        (10, 100_000),
        (100, 1_000_000),
    )


def test_bounded_profile_records_pragmas_sizes_latencies_and_index_plans() -> None:
    report = run_performance_profile("smoke", scales=((1, 100),))

    assert report["broker_dependency"] == "NONE"
    assert report["performance_budget"]["status"] == "PASSED"
    assert report["performance_budget"]["violations"] == []
    scale = report["scales"][0]
    assert (scale["bot_count"], scale["transition_count"]) == (1, 100)
    assert scale["pragmas"]["journal_mode"] == "wal"
    assert scale["pragmas"]["synchronous"] == 2
    assert scale["sizes_bytes"]["database"] > 0
    assert scale["sizes_bytes"]["mirror"] > 0
    assert scale["latencies"]["account_snapshot"]["sample_count"] == 12
    assert any("ix_custody_transitions_strategy_sequence" in detail for detail in scale["query_plans"]["bot_timeline"])
    markdown = qualification_markdown(report)
    assert "| 1 | 100 |" in markdown
    assert "offline, batched, hash-chained fixture builder" in markdown
    assert "Performance budget: `PASSED`" in markdown


def test_synthetic_5s_composition_preserves_exact_minute_ohlcv() -> None:
    aggregate = PolygonMinuteAggregate(
        timestamp=1_736_755_200_000,
        open=Decimal("578.23"),
        high=Decimal("579.01"),
        low=Decimal("576.24"),
        close=Decimal("576.42"),
        volume=8_162,
    )

    raw = synthesize_polygon_5s_bars(aggregate)

    assert len(raw) == 12
    assert tuple(bar.time for bar in raw) == tuple(aggregate.timestamp + index * 5_000 for index in range(12))
    assert raw[0].open == aggregate.open
    assert max(bar.high for bar in raw) == aggregate.high
    assert min(bar.low for bar in raw) == aggregate.low
    assert raw[-1].close == aggregate.close
    assert sum(bar.volume for bar in raw) == aggregate.volume


def test_polygon_fixture_replays_through_live_feed_path() -> None:
    evidence = run_polygon_replay(_POLYGON_FIXTURE, replayed_minute_count=3)

    assert evidence.fixture_bar_count == 4_062
    assert evidence.replayed_minute_count == 3
    assert evidence.synthesized_5s_count == 48
    assert evidence.emitted_minute_count == 3
    assert evidence.decision_count == 3
    assert evidence.decision_outcomes == ("HOLD", "HOLD", "HOLD")
    assert evidence.aggregation_path == ("stream_minute_bars/aggregate_realtime_bar/IbkrMarketDataFeed")
    assert evidence.timestamp_contract == "int64_ms_utc"
    assert evidence.decision_consumer_path == ("strategy_evaluations/DeploymentValidationDecisionKernel")


def test_synthetic_rehearsal_uses_protected_generation_and_matches_recovery_heads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = {
        scenario.scenario_id: SyntheticTestOutcome(
            status=("PENDING_CORE_VALIDATION" if scenario.category is SyntheticScenarioCategory.RECOVERY else "PASSED"),
            evidence_refs=scenario.test_refs,
            started_at_ms=1_786_140_000_000,
            completed_at_ms=1_786_140_000_001,
            ui_correlation=(
                SyntheticUiCorrelationEvidence(
                    campaign_id="frontend-browser-network-correlation-1413",
                    test_ref="Frontend/tests/e2e/alpaca-clerk-ui-correlation.spec.ts::campaign",
                    command=("npx", "playwright", "test"),
                    git_commit_sha="1" * 40,
                    test_source_sha256="2" * 64,
                    campaign_contract_sha256="7" * 64,
                    executed_at_ms=1_786_140_000_000,
                    duration_ms=1,
                    stdout_sha256="3" * 64,
                    stderr_sha256="4" * 64,
                    evidence_receipt_sha256="5" * 64,
                    interaction_count=25,
                    page_load_count=5,
                    browser_reload_count=4,
                    evidence_request_count=25,
                    evidence_response_proof_reference_count=25,
                    native_event_source_connection_count=25,
                    observed_revision_count=25,
                    revision_sequence=(41, 41, 42, 42, 43),
                    correlation_records=_ui_correlation_records(),
                    lifecycle_request_count=0,
                )
                if scenario.category is SyntheticScenarioCategory.UI
                else None
            ),
        )
        for scenario in SYNTHETIC_REHEARSAL_SCENARIOS
    }
    production_root = tmp_path / "production"
    production_root.mkdir()
    monkeypatch.setattr(
        qualification,
        "_assert_qualification_storage_boundary",
        lambda _qualification, _production: QualificationStorageBoundary(
            filesystem_type="xfs",
            qualification_mount_id=1415,
            production_mount_id=1,
            qualification_mount_access="READ_WRITE",
            production_mount_access="READ_ONLY",
            mount_identity_separated=True,
            production_authority_root=production_root,
        ),
    )

    report = run_synthetic_custody_rehearsal(
        artifacts_root=tmp_path,
        production_artifacts_root=production_root,
        fixture_directory=_POLYGON_FIXTURE,
        production_account_id="PRODUCTION-G1",
        test_outcomes=outcomes,
        generated_at_ms=1_786_140_000_000,
    )

    assert report.live_environment_status == "NOT_RUN"
    assert report.adr_0035_status == "PROPOSED"
    assert report.overall_status == ("REHEARSED_WITH_LIMITATIONS_PENDING_LIVE_CONFIRMATION")
    assert report.protected_generation.production_generation_opened_read_write is False
    assert report.protected_generation.production_mount_access == "READ_ONLY"
    assert report.protected_generation.qualification_mount_access == "READ_WRITE"
    assert report.protected_generation.mount_identity_separated is True
    assert report.protected_generation.account_id != "PRODUCTION-G1"
    assert report.recovery.integrity_check == "ok"
    assert report.recovery.hash_head_before == report.recovery.hash_head_after_restore
    assert report.recovery.hash_head_before == report.recovery.hash_head_after_rebuild
    by_id = {row.scenario_id: row for row in report.scenarios}
    for scenario in SYNTHETIC_REHEARSAL_SCENARIOS:
        assert by_id[scenario.scenario_id].evidence_refs[: len(scenario.test_refs)] == (scenario.test_refs)
    for scenario_id in (
        "polygon_replay_closed_minutes",
        "feed_one_print_stall",
        "feed_never_first_bar",
        "evidence_controls_read_only",
    ):
        assert by_id[scenario_id].worksheet.clerk_observed_at_ms is None
        assert tuple(item.code for item in by_id[scenario_id].limitations) == (
            "CLERK_OBSERVATION_CLOCK_NOT_APPLICABLE",
        )
    recovery_clocks = by_id["recovery_ceremony"].worksheet
    assert recovery_clocks.source_event_at_ms is not None
    assert recovery_clocks.clerk_observed_at_ms is not None
    assert recovery_clocks.recorded_at_ms is not None
    assert recovery_clocks.source_event_at_ms <= recovery_clocks.clerk_observed_at_ms <= recovery_clocks.recorded_at_ms

    for suffix, failure_origin, expected_cause, expected_class in (
        (
            "infrastructure-failure",
            SyntheticTestFailureOrigin.INFRASTRUCTURE_OR_TOOLING_FAILURE,
            "INFRASTRUCTURE_OR_TOOLING_FAILURE",
            "INFRA_ABORT",
        ),
        (
            "observed-invariant-failure",
            SyntheticTestFailureOrigin.OBSERVED_INVARIANT_FAILURE,
            "BROKER_MUTATION_WITHOUT_FINALIZED_INTENT",
            "CUSTODY_ABORT",
        ),
    ):
        failed_outcomes = {
            **outcomes,
            SyntheticScenarioId.LOST_SUBMIT_EXACT_IDENTITY: SyntheticTestOutcome(
                status="FAILED",
                evidence_refs=("tests/synthetic/test_lost_submit.py",),
                log_refs=(f"pytest:lost_submit_exact_identity:{suffix}",),
                failure_detail=(
                    "command exceeded the bounded 300s timeout"
                    if failure_origin == SyntheticTestFailureOrigin.INFRASTRUCTURE_OR_TOOLING_FAILURE
                    else "broker mutation landed without a finalized local intent"
                ),
                failure_origin=failure_origin,
            ),
        }
        failed_report = run_synthetic_custody_rehearsal(
            artifacts_root=tmp_path / suffix,
            production_artifacts_root=production_root,
            fixture_directory=_POLYGON_FIXTURE,
            production_account_id="PRODUCTION-G1",
            test_outcomes=failed_outcomes,
            generated_at_ms=1_786_140_100_000,
        )
        failed_row = next(
            row for row in failed_report.scenarios if row.scenario_id == SyntheticScenarioId.LOST_SUBMIT_EXACT_IDENTITY
        )

        assert failed_row.status == "ABORTED"
        assert failed_row.abort_cause == expected_cause
        assert failed_row.abort_class == expected_class


def test_polygon_core_failure_is_classified_as_feed_abort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        qualification,
        "run_polygon_replay",
        lambda _fixture: (_ for _ in ()).throw(ValueError("bad fixture")),
    )

    with pytest.raises(SyntheticRehearsalPhaseError) as captured:
        run_synthetic_custody_rehearsal(
            artifacts_root=tmp_path,
            production_artifacts_root=tmp_path,
            fixture_directory=_POLYGON_FIXTURE,
            production_account_id="PRODUCTION-G1",
            test_outcomes={},
        )

    assert captured.value.abort_class == "FEED_ABORT"
    assert captured.value.phase == "POLYGON_REPLAY"


def test_storage_boundary_failure_is_classified_as_infrastructure_abort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qualification, "run_polygon_replay", lambda _fixture: object())

    with pytest.raises(SyntheticRehearsalPhaseError) as captured:
        run_synthetic_custody_rehearsal(
            artifacts_root=tmp_path,
            production_artifacts_root=tmp_path,
            fixture_directory=_POLYGON_FIXTURE,
            production_account_id="PRODUCTION-G1",
            test_outcomes={},
        )

    assert captured.value.abort_class == "INFRA_ABORT"
    assert captured.value.phase == "PROTECTED_STORAGE_OR_RECOVERY_TOOLING"


def test_recovery_hash_failure_is_classified_as_custody_abort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qualification, "run_polygon_replay", lambda _fixture: object())
    monkeypatch.setattr(
        storage_recovery,
        "run_protected_generation",
        lambda **_kwargs: (_ for _ in ()).throw(SyntheticRecoveryCustodyError("restore hash head disagreed")),
    )

    with pytest.raises(SyntheticRehearsalPhaseError) as captured:
        run_synthetic_custody_rehearsal(
            artifacts_root=tmp_path,
            production_artifacts_root=tmp_path,
            fixture_directory=_POLYGON_FIXTURE,
            production_account_id="PRODUCTION-G1",
            test_outcomes={},
        )

    assert captured.value.abort_class == "CUSTODY_ABORT"
    assert captured.value.phase == "RECOVERY_INTEGRITY"


def test_protected_generation_does_not_infer_custody_from_refusal_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts_root = tmp_path / "qualification"
    production_root = tmp_path / "production"
    artifacts_root.mkdir()
    production_root.mkdir()
    monkeypatch.setattr(
        storage_recovery,
        "create_verified_backup",
        lambda **_kwargs: (_ for _ in ()).throw(RecoveryRefused("temporary hash tooling failure")),
    )

    with pytest.raises(RecoveryRefused, match="temporary hash tooling failure"):
        storage_recovery.run_protected_generation(
            artifacts_root=artifacts_root,
            production_artifacts_root=production_root,
            production_account_id="PRODUCTION-G1",
            generated_at_ms=1_786_140_000_000,
            storage_boundary_resolver=lambda _qualification, _production: QualificationStorageBoundary(
                filesystem_type="xfs",
                qualification_mount_id=1415,
                production_mount_id=1,
                qualification_mount_access="READ_WRITE",
                production_mount_access="READ_ONLY",
                mount_identity_separated=True,
                production_authority_root=production_root,
            ),
        )


def test_protected_generation_promotes_typed_integrity_refusal_to_custody(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts_root = tmp_path / "qualification"
    production_root = tmp_path / "production"
    artifacts_root.mkdir()
    production_root.mkdir()
    monkeypatch.setattr(
        storage_recovery,
        "create_verified_backup",
        lambda **_kwargs: (_ for _ in ()).throw(
            RecoveryRefused(
                "opaque recovery refusal",
                reason=(RecoveryRefusalReason.INTEGRITY_OR_IDENTITY_DISAGREEMENT),
            )
        ),
    )

    with pytest.raises(
        SyntheticRecoveryCustodyError,
        match="opaque recovery refusal",
    ):
        storage_recovery.run_protected_generation(
            artifacts_root=artifacts_root,
            production_artifacts_root=production_root,
            production_account_id="PRODUCTION-G1",
            generated_at_ms=1_786_140_000_000,
            storage_boundary_resolver=lambda _qualification, _production: QualificationStorageBoundary(
                filesystem_type="xfs",
                qualification_mount_id=1415,
                production_mount_id=1,
                qualification_mount_access="READ_WRITE",
                production_mount_access="READ_ONLY",
                mount_identity_separated=True,
                production_authority_root=production_root,
            ),
        )


@pytest.mark.parametrize(
    "created_at_ms",
    (None, "1786140000000", 1_786_140_000_000.0, False),
)
def test_protected_generation_treats_invalid_backup_clock_as_infrastructure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    created_at_ms: object,
) -> None:
    artifacts_root = tmp_path / "qualification"
    production_root = tmp_path / "production"
    artifacts_root.mkdir()
    production_root.mkdir()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"created_at_ms": created_at_ms}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        storage_recovery,
        "create_verified_backup",
        lambda **_kwargs: SimpleNamespace(
            manifest_path=manifest_path,
            bundle_path=tmp_path / "bundle",
            snapshot_sha256="1" * 64,
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="verified backup manifest has no integer creation clock",
    ):
        storage_recovery.run_protected_generation(
            artifacts_root=artifacts_root,
            production_artifacts_root=production_root,
            production_account_id="PRODUCTION-G1",
            generated_at_ms=1_786_140_000_000,
            storage_boundary_resolver=lambda _qualification, _production: QualificationStorageBoundary(
                filesystem_type="xfs",
                qualification_mount_id=1415,
                production_mount_id=1,
                qualification_mount_access="READ_WRITE",
                production_mount_access="READ_ONLY",
                mount_identity_separated=True,
                production_authority_root=production_root,
            ),
        )


def test_mount_topology_proves_separate_rw_qualification_and_ro_production() -> None:
    boundary = prove_mount_topology(
        qualification_root=Path("/qualification"),
        production_root=Path("/production"),
        mountinfo=(
            "10 1 0:1 / / rw,relatime - xfs /dev/root rw\n"
            "11 10 0:2 / /qualification rw,relatime - xfs volume-qual rw\n"
            "12 10 0:3 / /production ro,relatime - xfs volume-prod rw\n"
        ),
    )

    assert boundary.qualification_mount_id == 11
    assert boundary.production_mount_id == 12
    assert boundary.production_mount_access == "READ_ONLY"
    assert boundary.mount_identity_separated is True


@pytest.mark.parametrize(
    "mountinfo",
    (
        (
            "10 1 0:1 / / rw,relatime - xfs /dev/root rw\n"
            "11 10 0:2 / /qualification rw,relatime - xfs volume-qual rw\n"
            "12 10 0:3 / /production rw,relatime - xfs volume-prod rw\n"
        ),
        ("10 1 0:1 / / rw,relatime - xfs /dev/root rw\n11 10 0:2 / /qualification rw,relatime - xfs volume-qual rw\n"),
    ),
)
def test_mount_topology_rejects_writable_or_shared_production(
    mountinfo: str,
) -> None:
    with pytest.raises(RuntimeError):
        prove_mount_topology(
            qualification_root=Path("/qualification"),
            production_root=(Path("/production") if "/production " in mountinfo else Path("/qualification/production")),
            mountinfo=mountinfo,
        )
