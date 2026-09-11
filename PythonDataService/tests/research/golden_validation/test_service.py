"""Golden Validation preserves computed evidence separately from human judgment."""

from __future__ import annotations

import json

import pytest

from app.research.backtest_runs import repository as backtest_repo
from app.research.backtest_runs.records import record_from_payload
from app.research.golden_validation import service
from tests.research.backtest_runs.payloads import engine_payload, lean_payload

pytestmark = pytest.mark.asyncio


async def _run(conn, payload: dict) -> int:
    return (await backtest_repo.insert_run(conn, record_from_payload(payload))).run_id


async def _designate(conn, run_id: int, unique: str) -> service.GoldenValidationDossier:
    return await service.designate(
        conn,
        source_run_id=run_id,
        command_id=f"designate-{unique}",
        label="Chosen baseline",
        rationale="Parameters selected after research review.",
        actor="local:reviewer",
    )


def _certificate_payload(*, group: str, left: int, right: int, status: str) -> dict:
    divergences = (
        [{"category": "DECISION_MISMATCH", "trade_number": 1, "ms_utc": 1, "message": "one trade differs"}]
        if status == "diverged"
        else []
    )
    return {
        "schema_version": 3,
        "parity_group_id": group,
        "left_execution_id": left,
        "right_execution_id": right,
        "engines": {"left": "python", "right": "lean"},
        "status": status,
        "reason": "trade_reconciliation_diverged" if divergences else None,
        "tolerances": {"fill_price_atol": "0.01"},
        "native_metric_parity": {"status": "match"},
        "readiness_parity": {"status": "match"},
        "input_parity": {"status": "match", "fixture_id": "fixture", "fixture_sha256": "a" * 64},
        "parameter_parity": {"status": "match"},
        "program_version_parity": {
            "status": "match",
            "left_program_version": "ema-signal-v1",
            "right_program_version": "ema-signal-v1",
        },
        "divergences": divergences,
        "counts_by_category": {"DECISION_MISMATCH": 1} if divergences else {},
        "computed_at_ms": 1,
    }


async def test_designation_freezes_the_exact_case_and_missing_evidence_is_explicit(conn, unique: str) -> None:
    run_id = await _run(conn, engine_payload(symbol=unique))

    dossier = await _designate(conn, run_id, unique)

    assert dossier.state == "candidate"
    assert dossier.evidence.state == "missing"
    assert dossier.validation_case["source_run_id"] == run_id
    assert dossier.validation_case["symbol"] == unique.upper()
    assert dossier.validation_case["parameters"] == {"symbol": unique.upper(), "gap_bps": 0.0}
    assert dossier.validation_case["window"] == {
        "start_ms": 1736139600000,
        "end_ms": 1736485200000,
        "timespan": "minute",
    }
    assert dossier.validation_case["strategy"] == {"name": "ema_crossover_signal", "program_version": None}
    assert dossier.validation_case["execution"]["fill_mode"] == "signal_bar_close"
    assert len(dossier.evidence.revision) == 64

    same = await service.designate(
        conn,
        source_run_id=run_id,
        command_id=f"another-designation-{unique}",
        label="ignored because immutable",
        rationale="The source run already names the same immutable case.",
        actor="local:other",
    )
    assert same.golden_run.id == dossier.golden_run.id
    assert same.golden_run.label == "Chosen baseline"


async def test_human_can_accept_missing_evidence_only_as_a_visible_manual_override(conn, unique: str) -> None:
    run_id = await _run(conn, engine_payload(symbol=unique))
    designated = await _designate(conn, run_id, unique)

    accepted = await service.review(
        conn,
        golden_run_id=designated.golden_run.id,
        command_id=f"review-{unique}",
        expected_evidence_revision=designated.evidence.revision,
        decision="accept",
        reason="Proceed for paper observation while the retained evidence gap is understood.",
        quantconnect_backtest_id=None,
        authorized_program_version="ema-crossover-signal/v1",
        actor="local:reviewer",
    )

    assert accepted.state == "accepted_manual_override"
    assert accepted.latest_review is not None
    assert accepted.latest_review.evidence_state == "missing"
    assert accepted.latest_review.classification == "manual_override"
    assert json.loads(accepted.latest_review.evidence_json)["computed_state"] == "missing"
    assert accepted.review_is_current is True


@pytest.mark.parametrize(
    ("parity_status", "expected_evidence", "expected_classification"),
    [
        ("agree", "agreement", "engine_agreement"),
        ("diverged", "deviations", "reviewed_deviations"),
    ],
)
async def test_acceptance_classification_never_rewrites_the_computed_verdict(
    conn,
    unique: str,
    parity_status: str,
    expected_evidence: str,
    expected_classification: str,
) -> None:
    group = f"golden-pg-{unique}-{parity_status}"
    left = await _run(
        conn,
        engine_payload(
            symbol=unique,
            parity_group_id=group,
            requested_engine="both",
            program_version="ema-signal-v1",
        ),
    )
    right = await _run(conn, lean_payload(f"lean-{group}", symbol=unique, parity_group_id=group))
    await backtest_repo.freeze_parity_verdict(
        conn,
        parity_group_id=group,
        left_run_id=left,
        right_run_id=right,
        status=parity_status,
        verdict_json=json.dumps(
            _certificate_payload(group=group, left=left, right=right, status=parity_status)
        ),
    )
    designated = await _designate(conn, left, f"{unique}-{parity_status}")

    accepted = await service.review(
        conn,
        golden_run_id=designated.golden_run.id,
        command_id=f"review-{unique}-{parity_status}",
        expected_evidence_revision=designated.evidence.revision,
        decision="accept",
        reason="Reviewed the exact evidence and accept this qualification disposition.",
        quantconnect_backtest_id=f"optional-qc-{unique}",
        authorized_program_version=None,
        actor="local:reviewer",
    )

    assert accepted.evidence.state == expected_evidence
    assert accepted.evidence.payload["parity_verdict"]["status"] == parity_status
    assert accepted.latest_review is not None
    assert accepted.latest_review.evidence_state == expected_evidence
    assert accepted.latest_review.classification == expected_classification


async def test_review_refuses_stale_evidence_and_command_reuse_with_different_meaning(conn, unique: str) -> None:
    group = f"stale-pg-{unique}"
    run_id = await _run(conn, engine_payload(symbol=unique, parity_group_id=group, requested_engine="both"))
    await backtest_repo.create_parity_verdict(
        conn,
        parity_group_id=group,
        left_run_id=run_id,
        status="pending",
        verdict_json='{"status":"pending"}',
    )
    designated = await _designate(conn, run_id, unique)
    await backtest_repo.mark_parity_failed(conn, group, status="run_failed", detail="LEAN did not finish")

    with pytest.raises(service.StaleEvidenceError) as stale:
        await service.review(
            conn,
            golden_run_id=designated.golden_run.id,
            command_id=f"stale-review-{unique}",
            expected_evidence_revision=designated.evidence.revision,
            decision="reject",
            reason="This form displayed the earlier pending evidence.",
            quantconnect_backtest_id=None,
            authorized_program_version=None,
            actor="local:reviewer",
        )
    assert stale.value.current_revision != designated.evidence.revision

    with pytest.raises(service.CommandConflictError):
        await service.designate(
            conn,
            source_run_id=run_id,
            command_id=f"designate-{unique}",
            label="different command payload",
            rationale="Reusing a command id must not change immutable evidence.",
            actor="local:reviewer",
        )


async def test_only_python_runs_can_be_designated_and_linked_runs_are_retained(conn, unique: str) -> None:
    group = f"retained-pg-{unique}"
    left = await _run(
        conn,
        engine_payload(
            symbol=unique,
            parity_group_id=group,
            requested_engine="both",
            program_version="ema-signal-v1",
        ),
    )
    right = await _run(conn, lean_payload(f"lean-{group}", symbol=unique, parity_group_id=group))
    await backtest_repo.freeze_parity_verdict(
        conn,
        parity_group_id=group,
        left_run_id=left,
        right_run_id=right,
        status="agree",
        verdict_json=json.dumps(_certificate_payload(group=group, left=left, right=right, status="agree")),
    )

    with pytest.raises(service.GoldenRunIneligibleError):
        await _designate(conn, right, f"lean-{unique}")

    designated = await _designate(conn, left, unique)
    assert await backtest_repo.delete_run(conn, right) == "golden_validation_evidence"
    accepted = await service.review(
        conn,
        golden_run_id=designated.golden_run.id,
        command_id=f"retain-review-{unique}",
        expected_evidence_revision=designated.evidence.revision,
        decision="accept",
        reason="The engines agree for this exact case.",
        quantconnect_backtest_id=None,
        authorized_program_version=None,
        actor="local:reviewer",
    )
    assert accepted.state == "accepted_engine_agreement"
    assert await backtest_repo.delete_run(conn, left) == "golden_validation_evidence"
    assert await backtest_repo.delete_run(conn, right) == "golden_validation_evidence"
