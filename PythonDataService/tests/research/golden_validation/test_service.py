"""Golden Validation preserves computed evidence separately from human judgment."""

from __future__ import annotations

import asyncio
import json

import asyncpg
import pytest

from app.research.backtest_runs import repository as backtest_repo
from app.research.backtest_runs.records import record_from_payload
from app.research.golden_validation import repository as golden_repo
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


class _QueryCountingConnection:
    """Record real asyncpg round trips while delegating all database work."""

    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection
        self.fetch_queries: list[str] = []
        self.fetchrow_queries: list[str] = []

    async def fetch(self, query: str, *args: object, **kwargs: object) -> list[asyncpg.Record]:
        self.fetch_queries.append(query)
        return await self._connection.fetch(query, *args, **kwargs)

    async def fetchrow(
        self,
        query: str,
        *args: object,
        **kwargs: object,
    ) -> asyncpg.Record | None:
        self.fetchrow_queries.append(query)
        return await self._connection.fetchrow(query, *args, **kwargs)


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
        "native_metric_parity": {
            "status": "match",
            "contract_id": "lean-native-statistics-commit",
            "source_commit": "fixture-commit",
            "absolute_tolerance": 0.00005,
            "native_metric_count": 66,
            "formatted_metric_count": 25,
            "divergence_count": 0,
        },
        "readiness_parity": {"status": "match", "compared_field_count": 17, "mismatched_fields": []},
        "input_parity": {
            "status": "match",
            "fixture_id": "fixture",
            "fixture_sha256": "a" * 64,
            "compared_field_count": 16,
            "mismatched_fields": [],
        },
        "parameter_parity": {"status": "match", "compared_field_count": 2, "mismatched_fields": []},
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
    run_id = await _run(conn, engine_payload(symbol=unique, program_version=None))

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


async def test_deployment_scope_lookup_is_exact_and_not_a_history_page(conn, unique: str) -> None:
    exact_run = await _run(conn, engine_payload(symbol=unique))
    exact = await _designate(conn, exact_run, f"exact-{unique}")
    drifted_run = await _run(
        conn,
        engine_payload(
            symbol=unique,
            parameters={"symbol": unique.upper(), "gap_bps": 25.0},
        ),
    )
    await _designate(conn, drifted_run, f"drifted-{unique}")

    candidates = await service.find_deployment_scope_dossiers(
        conn,
        strategy_name="ema_crossover_signal",
        symbol=unique.upper(),
        parameters={"symbol": unique.upper(), "gap_bps": 0.0},
    )
    absent_scope = await service.find_deployment_scope_dossiers(
        conn,
        strategy_name="ema_crossover_signal",
        symbol=unique.upper(),
        parameters={"symbol": unique.upper(), "gap_bps": 50.0},
    )

    assert [item.golden_run.id for item in candidates.dossiers] == [exact.golden_run.id]
    assert candidates.strategy_has_golden_runs is True
    assert absent_scope.dossiers == ()
    assert absent_scope.strategy_has_golden_runs is True


async def test_deployment_scope_prefers_the_newest_latest_review_not_designation(conn, unique: str) -> None:
    """A later acceptance must supersede an earlier designation's priority."""
    first = await _designate(conn, await _run(conn, engine_payload(symbol=unique)), f"first-{unique}")
    second = await _designate(conn, await _run(conn, engine_payload(symbol=unique)), f"second-{unique}")
    assert first.golden_run.designated_at_ms <= second.golden_run.designated_at_ms

    for dossier, reviewed_at_ms in ((second, 100), (first, 200)):
        inserted = await golden_repo.insert_review(
            conn,
            golden_run_id=dossier.golden_run.id,
            command_id=f"review-order-{dossier.golden_run.id}-{unique}",
            command_sha256="a" * 64,
            expected_evidence_revision=dossier.evidence.revision,
            decision="accept",
            classification="manual_override",
            evidence_state=dossier.evidence.state,
            parity_verdict_id=None,
            evidence_json=json.dumps(dossier.evidence.payload),
            evidence_sha256="b" * 64,
            reason="Reviewed for ordering.",
            quantconnect_backtest_id=None,
            authorized_program_version=None,
            reviewed_by="local:reviewer",
            reviewed_at_ms=reviewed_at_ms,
        )
        assert inserted is not None

    candidates = await service.find_deployment_scope_dossiers(
        conn,
        strategy_name="ema_crossover_signal",
        symbol=unique.upper(),
        parameters={"symbol": unique.upper(), "gap_bps": 0.0},
    )

    assert [item.golden_run.id for item in candidates.dossiers] == [
        first.golden_run.id,
        second.golden_run.id,
    ]


async def test_catalog_dossiers_batch_reviews_and_parity_evidence(conn, unique: str) -> None:
    """Catalog data remains equivalent to normal loading with constant DB round trips.

    The query count is measured at the actual asyncpg connection boundary, not
    by replacing repository functions.  Four accepted paired cases must still
    produce the same two fetches as three: the catalog projection plus its
    single parity-verdict batch.
    """

    async def create_accepted_paired_case(index: int) -> service.GoldenValidationDossier:
        group = f"catalog-batch-{unique}-{index}"
        left = await _run(
            conn,
            engine_payload(
                symbol=unique,
                parameters={"symbol": unique.upper(), "gap_bps": float(index)},
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
        designated = await _designate(conn, left, f"catalog-batch-{unique}-{index}")
        return await service.review(
            conn,
            golden_run_id=designated.golden_run.id,
            command_id=f"catalog-review-{unique}-{index}",
            expected_evidence_revision=designated.evidence.revision,
            decision="accept",
            reason="Approved for the catalog projection.",
            quantconnect_backtest_id=None,
            authorized_program_version=None,
            actor="local:reviewer",
        )

    async def assert_batched_catalog(expected_case_count: int) -> None:
        counting_conn = _QueryCountingConnection(conn)
        catalog_dossiers = await service.list_latest_accepted_dossiers(
            counting_conn,  # type: ignore[arg-type]
            symbol=unique.upper(),
        )
        normal_dossiers = await service.list_dossiers(
            conn,
            strategy_name="ema_crossover_signal",
            symbol=unique.upper(),
            limit=expected_case_count,
        )

        assert len(catalog_dossiers) == expected_case_count
        normal_by_id = {dossier.golden_run.id: dossier for dossier in normal_dossiers}
        assert {dossier.golden_run.id for dossier in catalog_dossiers} == set(normal_by_id)
        for dossier in catalog_dossiers:
            normally_loaded = normal_by_id[dossier.golden_run.id]
            assert dossier.validation_case == normally_loaded.validation_case
            assert dossier.evidence == normally_loaded.evidence
            assert dossier.latest_review == normally_loaded.latest_review
            assert dossier.review_is_current == normally_loaded.review_is_current
            assert dossier.state == normally_loaded.state

        assert len(counting_conn.fetch_queries) == 2
        assert counting_conn.fetchrow_queries == []
        assert "research_validation_golden_runs" in counting_conn.fetch_queries[0]
        assert "research_parity_verdicts" in counting_conn.fetch_queries[1]

    for index in range(3):
        await create_accepted_paired_case(index)
    await assert_batched_catalog(expected_case_count=3)

    await create_accepted_paired_case(3)
    await assert_batched_catalog(expected_case_count=4)


async def test_human_can_accept_missing_evidence_only_as_a_visible_manual_override(conn, unique: str) -> None:
    run_id = await _run(conn, engine_payload(symbol=unique, program_version=None))
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


async def test_rejected_review_cannot_authorize_a_program_version(conn, unique: str) -> None:
    run_id = await _run(conn, engine_payload(symbol=unique))
    designated = await _designate(conn, run_id, unique)

    with pytest.raises(service.GoldenRunIneligibleError, match="cannot authorize"):
        await service.review(
            conn,
            golden_run_id=designated.golden_run.id,
            command_id=f"reject-version-{unique}",
            expected_evidence_revision=designated.evidence.revision,
            decision="reject",
            reason="This evidence is not suitable for promotion.",
            quantconnect_backtest_id=None,
            authorized_program_version="ema-crossover-signal/v1",
            actor="local:reviewer",
        )


async def test_designation_refuses_without_waiting_when_companion_delete_owns_run_lock(
    conn,
    second_conn,
    unique: str,
) -> None:
    group = f"delete-wins-{unique}"
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

    async with second_conn.transaction():
        await second_conn.fetchval(
            "SELECT id FROM research_backtest_runs WHERE id = $1 FOR UPDATE",
            right,
        )
        with pytest.raises(service.GoldenRunIneligibleError, match="Retry the designation"):
            await asyncio.wait_for(_designate(conn, left, unique), timeout=1.0)

    assert await golden_repo.get_golden_run_by_source(conn, left) is None


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


@pytest.mark.parametrize("provenance", [None, {
    "schema_version": 1, "data_contract": "spec_local_zip_partial/v1",
    "statistics_basis": "paired_realized_trade_ledger/v1",
    "daily_return_convention": "platform_skip_first_session/v1",
}])
async def test_unknown_or_affected_conventions_need_deliberate_override_and_freeze_it(conn, unique: str, provenance) -> None:
    payload = engine_payload(symbol=unique, evidence_provenance_json=None if provenance is None else json.dumps(provenance))
    run_id = await _run(conn, payload)
    designated = await _designate(conn, run_id, unique)
    original_case = designated.golden_run.validation_case_json
    arguments = dict(
        golden_run_id=designated.golden_run.id, command_id=f"risk-review-{unique}",
        expected_evidence_revision=designated.evidence.revision, decision="accept",
        reason="I accept these recorded limitations for this exact configuration.",
        quantconnect_backtest_id=None, authorized_program_version=None, actor="test:reviewer",
    )
    with pytest.raises(service.GoldenRunIneligibleError, match="Manual override"):
        await service.review(conn, **arguments)
    accepted = await service.review(conn, **arguments, acknowledge_provenance_risk=True)
    assert accepted.latest_review.classification == "manual_override"
    assert accepted.review_is_current is True
    assert json.loads(accepted.latest_review.evidence_json)["acknowledge_provenance_risk"] is True
    assert accepted.golden_run.validation_case_json == original_case
    assert accepted.validation_case["evidence_provenance"] == (None if provenance is None else {**provenance, "data_availability_hash": None})
    repeated = await service.review(conn, **arguments, acknowledge_provenance_risk=True)
    assert repeated.latest_review.id == accepted.latest_review.id
    with pytest.raises(service.CommandConflictError):
        await service.review(conn, **arguments)


async def test_rerunning_does_not_mutate_or_accept_the_old_baseline(conn, unique: str) -> None:
    old = await _designate(conn, await _run(conn, engine_payload(symbol=unique, evidence_provenance_json=None)), unique)
    rerun = await _designate(conn, await _run(conn, engine_payload(symbol=unique)), unique + "-rerun")
    assert old.golden_run.id != rerun.golden_run.id
    assert old.evidence_applicability.status == "unknown"
    assert rerun.evidence_applicability.status == "current"
    assert rerun.latest_review is None
    preserved = await service.get_dossier(conn, old.golden_run.id)
    assert preserved.validation_case == old.validation_case
    assert preserved.reviews == old.reviews
