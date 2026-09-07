"""The parity verdict a LEAN companion freezes — ported from Backend.Tests ParityVerdictServiceTests."""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.research.backtest_runs import repository as repo
from app.research.backtest_runs.parity import compute_parity_verdict, settle_parity_for_lean_run
from app.research.backtest_runs.records import record_from_payload
from app.research.backtest_runs.repository import RunDetail, TradeRow
from app.utils.session_anchors import et_midnight_ms
from tests.research.backtest_runs.payloads import data_policy, engine_payload, lean_payload, trade

MATCHING_RUN_VERDICT = {
    "verdict_version": 2,
    "status": "complete",
    "composite": 76,
    "grade": "A",
    "signal": "Paper-trade",
    "red_flags": [],
    "dimensions": [{"key": "return_quality", "score": 70, "sub_scores": []}],
    "missing_metrics": [],
    "missing_required_metrics": [],
    "available_required_metrics": 17,
    "required_metrics": 17,
    "normalized_weights": False,
    "parity_signature": {
        "contract_id": "readiness-core-v2",
        "absolute_tolerance": "0.000000000001",
        "status": "complete",
        "composite": 76,
        "grade": "A",
        "signal": "Paper-trade",
        "red_flags": [],
        "missing_required_metrics": [],
        "available_required_metrics": 17,
        "required_metrics": 17,
        "normalized_weights": False,
        "required_inputs": [],
    },
}

MATCHING_NATIVE_STATISTICS = {
    "namespaces": {
        "status": "match",
        "contract_id": "lean-native-statistics-commit",
        "source_commit": "commit",
        "absolute_tolerance": 0.0000500001,
        "native_metric_count": 66,
        "formatted_metric_count": 25,
        "divergences": [],
    }
}

MATCHING_DATA_POLICY = data_policy("SPY", fixture_id="bar-store-v1-exact")


def _trade_row(number: int = 1, *, exit_price: float = 712.0) -> TradeRow:
    return TradeRow(
        id=number,
        trade_number=number,
        entry_ms=1_736_173_800_000,
        exit_ms=1_736_179_200_000,
        entry_price=710.0,
        exit_price=exit_price,
        quantity=10.0,
        pnl=(exit_price - 710.0) * 10,
        signal_reason="",
        is_synthetic_exit=False,
    )


def _run(run_id: int, source: str, **overrides) -> RunDetail:
    fields = dict(
        id=run_id,
        source=source,
        requested_engine="both",
        strategy_name="ema_crossover",
        symbol="SPY",
        lean_run_id=f"companion-{run_id}" if source == "lean-sidecar" else None,
        parameters_json='{"symbol": "SPY"}',
        start_ms=et_midnight_ms(date(2026, 1, 5)),
        end_ms=et_midnight_ms(date(2026, 1, 6)),
        fill_mode="signal_bar_close",
        timespan="minute",
        executed_at_ms=1,
        duration_ms=0,
        total_trades=1,
        winning_trades=1,
        losing_trades=0,
        win_rate=1.0,
        total_pnl=20.0,
        initial_cash=100_000.0,
        final_equity=100_020.0,
        total_fees=0.0,
        max_drawdown=0.0,
        sharpe_ratio=None,
        sortino_ratio=None,
        profit_factor=None,
        commission_per_order=0.0,
        brokerage_policy=None,
        lean_statistics_json=json.dumps(MATCHING_NATIVE_STATISTICS) if source == "lean-sidecar" else None,
        lean_analysis_json=None,
        run_verdict_json=json.dumps(MATCHING_RUN_VERDICT),
        verdict_version=2,
        verdict_grade="A",
        verdict_signal="Paper-trade",
        equity_curve_json=None,
        validation_analytics_json=None,
        insight_summary_json=None,
        metric_documentation_json=None,
        data_policy_json=json.dumps(MATCHING_DATA_POLICY),
        parity_group_id="pg-test",
        notes=None,
        trades=(_trade_row(),),
        trades_truncated=False,
        parity_verdicts=(),
    )
    fields.update(overrides)
    return RunDetail(**fields)


def _verdict(**right_overrides) -> tuple[str, dict]:
    left = _run(1, "engine")
    right = _run(2, "lean-sidecar", **right_overrides)
    verdict = compute_parity_verdict(parity_group_id="pg-test", left=left, right=right)
    return verdict.status, json.loads(verdict.verdict_json)


def test_no_divergences_agree_with_every_receipt_matching() -> None:
    status, verdict = _verdict()

    assert status == "agree" and verdict["status"] == "agree" and verdict["reason"] is None
    assert verdict["native_metric_parity"]["status"] == "match"
    assert verdict["readiness_parity"] == {
        "status": "match",
        "reason": None,
        "compared_field_count": 17,
        "mismatched_fields": [],
    }
    assert (
        verdict["input_parity"]["status"] == "match" and verdict["input_parity"]["fixture_id"] == "bar-store-v1-exact"
    )
    assert verdict["left_execution_id"] == 1 and verdict["right_execution_id"] == 2
    assert verdict["engines"] == {"left": "python", "right": "lean"} and verdict["tolerances"] == {
        "fill_price_atol": "0.01"
    }
    assert verdict["divergences"] == [] and verdict["counts_by_category"] == {} and verdict["computed_at_ms"] > 0


def test_a_trade_divergence_freezes_diverged_with_category_counts() -> None:
    status, verdict = _verdict(trades=(_trade_row(exit_price=712.5),))

    assert status == "diverged" and verdict["reason"] == "trade_reconciliation_diverged"
    assert "FILL_PRICE_DRIFT" in verdict["counts_by_category"]
    assert (
        verdict["divergences"][0]["category"] == "FILL_PRICE_DRIFT" and verdict["divergences"][0]["trade_number"] == 1
    )


def test_a_readiness_mismatch_names_the_signature_in_the_receipt() -> None:
    right_verdict = json.loads(json.dumps(MATCHING_RUN_VERDICT))
    right_verdict["grade"] = right_verdict["parity_signature"]["grade"] = "B"

    status, verdict = _verdict(run_verdict_json=json.dumps(right_verdict))

    assert status == "diverged" and verdict["reason"] == "production_readiness_mismatch"
    assert verdict["readiness_parity"]["mismatched_fields"] == ["parity_signature"]


def test_a_different_readiness_contract_version_is_unavailable_not_a_mismatch() -> None:
    right_verdict = json.loads(json.dumps(MATCHING_RUN_VERDICT))
    right_verdict["parity_signature"]["contract_id"] = "readiness-core-v3"

    status, verdict = _verdict(run_verdict_json=json.dumps(right_verdict))

    assert status == "unavailable"
    assert verdict["readiness_parity"]["status"] == "unavailable"
    assert verdict["readiness_parity"]["reason"] == "readiness_contract_version_differs"
    assert verdict["reason"] == "production_readiness_parity_unavailable"


def test_the_readiness_receipt_ignores_presentation_noise_outside_the_signature() -> None:
    right_verdict = json.loads(json.dumps(MATCHING_RUN_VERDICT))
    right_verdict["dimensions"][0]["score"] = 71

    status, verdict = _verdict(run_verdict_json=json.dumps(right_verdict))

    assert status == "agree" and verdict["readiness_parity"]["status"] == "match"


def test_legacy_verdicts_without_a_signature_compare_field_by_field() -> None:
    legacy = {key: value for key, value in MATCHING_RUN_VERDICT.items() if key != "parity_signature"}
    left = _run(1, "engine", run_verdict_json=json.dumps(legacy))
    right = _run(2, "lean-sidecar", run_verdict_json=json.dumps({**legacy, "grade": "B"}))

    verdict = json.loads(compute_parity_verdict(parity_group_id="pg-test", left=left, right=right).verdict_json)

    assert verdict["readiness_parity"] == {
        "status": "mismatch",
        "reason": "readiness_fields_differ",
        "compared_field_count": 12,
        "mismatched_fields": ["grade"],
    }


def test_a_native_metric_mismatch_freezes_diverged() -> None:
    native = json.loads(json.dumps(MATCHING_NATIVE_STATISTICS))
    native["namespaces"]["status"] = "mismatch"

    status, verdict = _verdict(lean_statistics_json=json.dumps(native))

    assert status == "diverged" and verdict["reason"] == "lean_native_metric_mismatch"


def test_a_missing_metric_receipt_is_unavailable_instead_of_a_false_agreement() -> None:
    status, verdict = _verdict(lean_statistics_json="{}")

    assert status == "unavailable" and verdict["reason"] == "lean_native_metric_parity_unavailable"
    assert verdict["native_metric_parity"]["reason"] == "lean_native_metric_receipt_missing"


def test_missing_and_unreadable_native_statistics_are_named() -> None:
    assert _verdict(lean_statistics_json=None)[1]["native_metric_parity"]["reason"] == "lean_statistics_missing"
    assert (
        _verdict(lean_statistics_json="{not json")[1]["native_metric_parity"]["reason"] == "lean_statistics_unreadable"
    )


def test_a_shared_fixture_mismatch_freezes_diverged_and_names_the_fixture() -> None:
    status, verdict = _verdict(data_policy_json=json.dumps(data_policy("SPY", fixture_id="bar-store-v1-changed")))

    assert status == "diverged" and verdict["reason"] == "compatibility_input_mismatch"
    assert verdict["input_parity"]["mismatched_fields"] == ["fixture_id"]
    assert verdict["input_parity"]["fixture_id"] == "bar-store-v1-changed"


def test_cash_window_and_fill_mode_are_compared_as_inputs() -> None:
    status, verdict = _verdict(initial_cash=50_000.0, end_ms=et_midnight_ms(date(2026, 1, 7)), fill_mode="lean-sidecar")

    assert status == "diverged"
    assert verdict["input_parity"]["mismatched_fields"] == ["initial_cash", "end_date", "fill_mode"]
    assert verdict["input_parity"]["compared_field_count"] == 15


def test_a_missing_data_policy_is_unavailable() -> None:
    status, verdict = _verdict(data_policy_json=None)

    assert status == "unavailable" and verdict["reason"] == "compatibility_input_parity_unavailable"


# ── Freezing against the database ────────────────────────────────────────


async def _seed_group(conn, unique: str, *, pending: str | None = "pending") -> tuple[str, int, int]:
    group = f"pg-{unique}"
    left = (
        await repo.insert_run(
            conn,
            record_from_payload(
                engine_payload(
                    symbol=unique,
                    parity_group_id=group,
                    requested_engine="both",
                    run_verdict_json=json.dumps(MATCHING_RUN_VERDICT),
                    data_policy_json=json.dumps(MATCHING_DATA_POLICY),
                    start_date="2026-01-05",
                    end_date="2026-01-06",
                    trades=[trade()],
                )
            ),
        )
    ).run_id
    right = (
        await repo.insert_run(
            conn,
            record_from_payload(
                lean_payload(
                    f"companion-{group}",
                    symbol=unique,
                    parity_group_id=group,
                    requested_engine="both",
                    fill_mode="signal_bar_close",
                    run_verdict_json=json.dumps(MATCHING_RUN_VERDICT),
                    lean_statistics=MATCHING_NATIVE_STATISTICS,
                    data_policy_json=json.dumps(MATCHING_DATA_POLICY),
                    start_date="2026-01-05",
                    end_date="2026-01-06",
                    trades=[trade()],
                )
            ),
        )
    ).run_id
    if pending is not None:
        await repo.create_parity_verdict(
            conn, parity_group_id=group, left_run_id=left, status="pending", verdict_json="{}"
        )
        if pending != "pending":
            await repo.mark_parity_failed(conn, group, status=pending, detail="seeded")
    return group, left, right


@pytest.mark.asyncio
async def test_freezing_agree_onto_the_pending_row(conn, unique: str) -> None:
    group, left, right = await _seed_group(conn, unique)

    assert await settle_parity_for_lean_run(right_run_id=right, parity_group_id=group) is True

    row = await repo.get_parity_verdict(conn, group)
    assert row is not None and row.status == "agree" and row.left_run_id == left and row.right_run_id == right
    assert json.loads(row.verdict_json)["input_parity"]["status"] == "match"


@pytest.mark.asyncio
@pytest.mark.parametrize("provisional", ["run_failed", "persist_failed"])
async def test_a_provisional_dispatch_failure_is_superseded_by_the_companion_that_landed(
    conn, unique: str, provisional: str
) -> None:
    """#1977: the dispatch mark claims no companion is coming; this one came."""
    group, left, right = await _seed_group(conn, f"{unique}{provisional[0]}", pending=provisional)

    assert await settle_parity_for_lean_run(right_run_id=right, parity_group_id=group) is True

    row = await repo.get_parity_verdict(conn, group)
    assert row is not None and row.status == "agree" and row.left_run_id == left and row.right_run_id == right


@pytest.mark.asyncio
async def test_a_computed_verdict_is_never_overwritten(conn, unique: str) -> None:
    group, _, right = await _seed_group(conn, unique)
    assert await settle_parity_for_lean_run(right_run_id=right, parity_group_id=group) is True

    assert await settle_parity_for_lean_run(right_run_id=right, parity_group_id=group) is False
    assert (await repo.get_parity_verdict(conn, group)).status == "agree"


@pytest.mark.asyncio
async def test_an_unavailable_disposition_is_never_overwritten(conn, unique: str) -> None:
    """A group that was never eligible for a companion keeps its honest reason."""
    group, left, right = await _seed_group(conn, unique, pending=None)
    await repo.create_parity_verdict(
        conn, parity_group_id=group, left_run_id=left, status="unavailable", verdict_json="{}"
    )

    assert await settle_parity_for_lean_run(right_run_id=right, parity_group_id=group) is False
    assert (await repo.get_parity_verdict(conn, group)).status == "unavailable"


@pytest.mark.asyncio
async def test_a_lost_pending_row_gets_the_terminal_verdict_inserted(conn, unique: str) -> None:
    group, _, right = await _seed_group(conn, unique, pending=None)

    assert await settle_parity_for_lean_run(right_run_id=right, parity_group_id=group) is True
    assert (await repo.get_parity_verdict(conn, group)).status == "agree"


@pytest.mark.asyncio
async def test_a_missing_python_run_leaves_the_group_without_a_verdict(conn, unique: str) -> None:
    group = f"pg-orphan-{unique}"
    right = (
        await repo.insert_run(
            conn, record_from_payload(lean_payload(f"companion-{group}", symbol=unique, parity_group_id=group))
        )
    ).run_id

    assert await settle_parity_for_lean_run(right_run_id=right, parity_group_id=group) is False
    assert await repo.get_parity_verdict(conn, group) is None


@pytest.mark.asyncio
async def test_a_failed_companion_settles_a_group_whose_pending_row_was_lost(conn, unique: str) -> None:
    """#1977 M2: the failure path self-heals like the comparison path does.

    ``record_parity_disposition_sync`` is best-effort, so a group with no
    verdict row at all is a live state. Settling through the one freeze path
    inserts the row rather than no-opping and leaving the group unsettled.
    """
    group, left, right = await _seed_group(conn, unique, pending=None)
    assert await repo.get_parity_verdict(conn, group) is None

    written = await settle_parity_for_lean_run(
        right_run_id=right, parity_group_id=group, failure_detail="No normalized/result.json"
    )

    assert written is True
    row = await repo.get_parity_verdict(conn, group)
    assert row is not None and row.status == "run_failed"
    assert row.left_run_id == left and row.right_run_id == right  # the failed row is linkable
    assert json.loads(row.verdict_json)["reason"] == "No normalized/result.json"


@pytest.mark.asyncio
async def test_a_failed_companion_does_not_compare_a_zero_trade_row(conn, unique: str) -> None:
    group, _, right = await _seed_group(conn, unique)

    await settle_parity_for_lean_run(
        right_run_id=right, parity_group_id=group, failure_detail="normalization_error: ValueError: boom"
    )

    row = await repo.get_parity_verdict(conn, group)
    assert row is not None and row.status == "run_failed"  # never 'diverged'


@pytest.mark.asyncio
async def test_a_landed_companions_failure_is_not_superseded_the_way_a_dispatch_mark_is(conn, unique: str) -> None:
    """Two verdicts read ``run_failed``; only one of them is a claim about the future.

    The dispatch mark says "no comparable companion is coming" before any
    companion row exists, so a row that lands disproves it. This one was
    written *from* a companion that landed and said it produced no comparable
    result — as settled as a comparison, and its ``right_run_id`` is what
    records the difference. A retry must not rewrite it, and a later differently
    shaped settle must not replace it.
    """
    group, left, right = await _seed_group(conn, unique)
    assert await settle_parity_for_lean_run(right_run_id=right, parity_group_id=group, failure_detail="boom") is True
    landed = await repo.get_parity_verdict(conn, group)
    assert landed is not None and landed.status == "run_failed" and landed.right_run_id == right

    # A redelivery of the same failure, and a comparison arriving afterwards,
    # both find the group already settled.
    assert (
        await settle_parity_for_lean_run(right_run_id=right, parity_group_id=group, failure_detail="boom again")
        is False
    )
    assert await settle_parity_for_lean_run(right_run_id=right, parity_group_id=group) is False

    row = await repo.get_parity_verdict(conn, group)
    assert row is not None and row.status == "run_failed" and row.left_run_id == left
    assert json.loads(row.verdict_json)["reason"] == "boom"  # the original reason, not the retry's


@pytest.mark.asyncio
async def test_a_group_with_no_python_run_stays_unsettled(conn, unique: str) -> None:
    group = f"pg-orphan-failed-{unique}"
    right = (
        await repo.insert_run(
            conn, record_from_payload(lean_payload(f"companion-{group}", symbol=unique, parity_group_id=group))
        )
    ).run_id

    assert await settle_parity_for_lean_run(right_run_id=right, parity_group_id=group, failure_detail="boom") is False
    assert await repo.get_parity_verdict(conn, group) is None
