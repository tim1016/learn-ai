"""Independent equation tests for the canonical execution-coverage set proof."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.execution_coverage import (
    CumulativeCoverageObservation,
    CumulativeRecoveryFill,
    ExactCoverageObservation,
    ExecutionCoverageIdentity,
    ExecutionCoverageSetCandidate,
    ExecutionCoverageSetProofRefusal,
    ExecutionCoverageSetProofRefusalReason,
    ExecutionCoverageSetProofSuccess,
    exact_replaces_cumulative,
    prove_execution_coverage_set,
)
from app.broker.alpaca.clerk.sqlite.facts import ExecutionSliceFilledFacts

EXPECTED_QTY_ATOL = 1e-9
EXPECTED_PRICE_ATOL = 1e-9
_GOLDEN_FIXTURE_DIRECTORY = (
    Path(__file__).parents[4] / "fixtures/golden/clerk-execution-coverage-set-proof/one_to_one"
)


def _identity(**changes: object) -> ExecutionCoverageIdentity:
    values = {
        "account_id": "PA-TEST",
        "authority_generation": 2,
        "database_identity_token": "database-identity",
        "order_ref": "order-1",
        "symbol": "SPY",
        "side": "BUY",
    }
    values.update(changes)
    return ExecutionCoverageIdentity(**values)


def _cumulative(
    source_id: str,
    quantity: float,
    price: float,
    *,
    identity: ExecutionCoverageIdentity | None = None,
) -> CumulativeCoverageObservation:
    return CumulativeCoverageObservation(
        source_id=source_id,
        identity=identity or _identity(),
        quantity=quantity,
        price=price,
    )


def _exact(
    source_id: str,
    quantity: float,
    price: float,
    *,
    identity: ExecutionCoverageIdentity | None = None,
    fee: float | None = None,
) -> ExactCoverageObservation:
    return ExactCoverageObservation(
        source_id=source_id,
        identity=identity or _identity(),
        quantity=quantity,
        price=price,
        fee=fee,
    )


def _candidate(
    *,
    cumulative: tuple[CumulativeCoverageObservation, ...] = (_cumulative("recovery-1", 1.0, 10.0),),
    prior: tuple[ExactCoverageObservation, ...] = (),
    incoming: ExactCoverageObservation | None = None,
    active_episode_ids: tuple[str, ...] = (),
    effective_exact_source_ids: frozenset[str] = frozenset(),
    unreadable_source_ids: tuple[str, ...] = (),
) -> ExecutionCoverageSetCandidate:
    return ExecutionCoverageSetCandidate(
        cumulative_recovery=cumulative,
        prior_quarantined_exact=prior,
        incoming_exact=incoming or _exact("execution-incoming", 1.0, 10.0),
        active_episode_ids=active_episode_ids,
        effective_exact_source_ids=effective_exact_source_ids,
        unreadable_source_ids=unreadable_source_ids,
    )


def _golden_one_to_one_candidate() -> tuple[ExecutionCoverageSetCandidate, dict[str, object]]:
    input_payload = json.loads((_GOLDEN_FIXTURE_DIRECTORY / "input.json").read_text(encoding="utf-8"))
    expected_payload = json.loads((_GOLDEN_FIXTURE_DIRECTORY / "output.json").read_text(encoding="utf-8"))
    identity = _identity(**input_payload["identity"])
    cumulative = tuple(
        _cumulative(
            row["source_id"],
            row["quantity"],
            row["price"],
            identity=identity,
        )
        for row in input_payload["cumulative_recovery"]
    )
    prior = tuple(
        _exact(
            row["source_id"],
            row["quantity"],
            row["price"],
            identity=identity,
            fee=row["fee"],
        )
        for row in input_payload["prior_quarantined_exact"]
    )
    incoming = input_payload["incoming_exact"]
    return (
        _candidate(
            cumulative=cumulative,
            prior=prior,
            incoming=_exact(
                incoming["source_id"],
                incoming["quantity"],
                incoming["price"],
                identity=identity,
                fee=incoming["fee"],
            ),
        ),
        expected_payload,
    )


def _aggregate(observations: tuple[CumulativeCoverageObservation | ExactCoverageObservation, ...]) -> tuple[float, float, float]:
    """Encode the issue's fsum equations independently of the production proof."""
    ordered = tuple(sorted(observations, key=lambda item: item.source_id))
    quantity = math.fsum(item.quantity for item in ordered)
    cost = math.fsum(item.quantity * item.price for item in ordered)
    return quantity, cost, cost / quantity


def _assert_success(result: object) -> ExecutionCoverageSetProofSuccess:
    assert isinstance(result, ExecutionCoverageSetProofSuccess)
    return result


def _assert_refusal(
    result: object,
    reason: ExecutionCoverageSetProofRefusalReason,
) -> None:
    assert isinstance(result, ExecutionCoverageSetProofRefusal)
    assert result.reason is reason


def test_prove_execution_coverage_set_accepts_one_exact_for_one_cumulative() -> None:
    candidate = _candidate()

    result = _assert_success(prove_execution_coverage_set(candidate))

    expected_exact = _aggregate((candidate.incoming_exact,))
    expected_cumulative = _aggregate(candidate.cumulative_recovery)
    assert (result.exact.quantity, result.exact.gross_cost, result.exact.vwap) == expected_exact
    assert (result.cumulative.quantity, result.cumulative.gross_cost, result.cumulative.vwap) == expected_cumulative
    assert abs(result.position_delta) < EXPECTED_QTY_ATOL
    assert abs(result.exact.quantity - result.cumulative.quantity) < EXPECTED_QTY_ATOL
    assert abs(result.exact.gross_cost - result.cumulative.gross_cost) <= (
        max(result.exact.quantity, result.cumulative.quantity) * EXPECTED_PRICE_ATOL
    )


def test_prove_execution_coverage_set_matches_the_one_to_one_golden_fixture() -> None:
    candidate, expected = _golden_one_to_one_candidate()

    result = _assert_success(prove_execution_coverage_set(candidate))

    tolerances = expected["tolerances"]
    assert expected["accepted"] is True
    for actual, expected_aggregate in (
        (result.exact, expected["exact"]),
        (result.cumulative, expected["cumulative"]),
    ):
        assert actual.quantity == pytest.approx(
            expected_aggregate["quantity"],
            abs=tolerances["quantity_atol"],
            rel=tolerances["rtol"],
        )
        assert actual.gross_cost == pytest.approx(
            expected_aggregate["gross_cost"],
            abs=tolerances["gross_cost_atol"],
            rel=tolerances["rtol"],
        )
        assert actual.vwap == pytest.approx(
            expected_aggregate["vwap"],
            abs=tolerances["price_atol"],
            rel=tolerances["rtol"],
        )
    assert result.position_delta == pytest.approx(
        expected["position_delta"],
        abs=tolerances["quantity_atol"],
        rel=tolerances["rtol"],
    )
    assert tuple(item.source_id for item in result.retained_exact_observations) == tuple(
        expected["retained_exact_source_ids"]
    )


@pytest.mark.parametrize(
    ("exact_quantity", "exact_price", "cumulative_quantity", "cumulative_price", "cumulative_side"),
    [
        (2.5, 101.25, 2.5, 101.25, "BUY"),
        (2.5, 101.25, 2.5, 101.250000002, "BUY"),
        (2.5, 101.25, 2.500000002, 101.25, "BUY"),
        (2.5, 101.25, 2.5, 101.25, "SELL"),
    ],
)
def test_s0_one_to_one_predicate_matches_canonical_set_proof_on_unambiguous_inputs(
    exact_quantity: float,
    exact_price: float,
    cumulative_quantity: float,
    cumulative_price: float,
    cumulative_side: str,
) -> None:
    exact = ExecutionSliceFilledFacts(
        execution_id="execution-1",
        symbol="SPY",
        side="BUY",
        slice_qty=exact_quantity,
        slice_price=exact_price,
        fee=None,
        fee_fidelity="unavailable",
        evidence_source="golden-fixture",
        source_event_at_ms=1_723_748_800_000,
    )
    cumulative = CumulativeRecoveryFill(
        fill_id="recovery-1",
        order_ref="order-1",
        quantity=cumulative_quantity,
        price=cumulative_price,
        side=cumulative_side,
    )
    cumulative_identity = _identity(side=cumulative_side)
    candidate = _candidate(
        cumulative=(
            _cumulative(
                "recovery-1",
                cumulative_quantity,
                cumulative_price,
                identity=cumulative_identity,
            ),
        ),
        incoming=_exact("execution-1", exact_quantity, exact_price),
    )

    canonical_result = prove_execution_coverage_set(candidate)

    assert exact_replaces_cumulative(exact=exact, cumulative=cumulative) is isinstance(
        canonical_result,
        ExecutionCoverageSetProofSuccess,
    )


def test_prove_execution_coverage_set_accepts_many_to_many_aggregate_equivalence() -> None:
    candidate = _candidate(
        cumulative=(
            _cumulative("recovery-b", 3.0, 12.0),
            _cumulative("recovery-a", 2.0, 10.0),
        ),
        prior=(_exact("execution-prior", 1.0, 10.0),),
        incoming=_exact("execution-incoming", 4.0, 11.5),
        active_episode_ids=("episode-1",),
    )

    result = _assert_success(prove_execution_coverage_set(candidate))

    assert result.exact == result.cumulative
    assert result.exact.quantity == 5.0
    assert result.exact.gross_cost == 56.0
    assert result.exact.vwap == 11.2
    assert result.retained_exact_observations == tuple(
        sorted((*candidate.prior_quarantined_exact, candidate.incoming_exact), key=lambda item: item.source_id)
    )


def test_prove_execution_coverage_set_refuses_partial_accumulation_then_accepts_completion() -> None:
    partial = _candidate(
        cumulative=(_cumulative("recovery-1", 10.0, 10.0),),
        prior=(_exact("execution-prior", 4.0, 10.0),),
        incoming=_exact("execution-incoming", 3.0, 10.0),
        active_episode_ids=("episode-1",),
    )

    _assert_refusal(
        prove_execution_coverage_set(partial),
        ExecutionCoverageSetProofRefusalReason.QUANTITY_MISMATCH,
    )

    completed = replace(partial, incoming_exact=_exact("execution-incoming", 6.0, 10.0))
    _assert_success(prove_execution_coverage_set(completed))


def test_prove_execution_coverage_set_refuses_quantity_overshoot() -> None:
    candidate = _candidate(
        cumulative=(_cumulative("recovery-1", 10.0, 10.0),),
        prior=(_exact("execution-prior", 7.0, 10.0),),
        incoming=_exact("execution-incoming", 4.0, 10.0),
        active_episode_ids=("episode-1",),
    )

    _assert_refusal(
        prove_execution_coverage_set(candidate),
        ExecutionCoverageSetProofRefusalReason.QUANTITY_MISMATCH,
    )


def test_prove_execution_coverage_set_is_deterministic_across_input_order() -> None:
    candidate = _candidate(
        cumulative=(
            _cumulative("recovery-b", 3.0, 12.0),
            _cumulative("recovery-a", 2.0, 10.0),
        ),
        prior=(
            _exact("execution-prior-b", 1.0, 10.0),
            _exact("execution-prior-a", 2.0, 12.0),
        ),
        incoming=_exact("execution-incoming", 2.0, 11.0),
        active_episode_ids=("episode-1",),
    )

    shuffled = replace(
        candidate,
        cumulative_recovery=tuple(reversed(candidate.cumulative_recovery)),
        prior_quarantined_exact=tuple(reversed(candidate.prior_quarantined_exact)),
    )

    assert prove_execution_coverage_set(candidate) == prove_execution_coverage_set(shuffled)


def test_prove_execution_coverage_set_pins_strict_quantity_boundary() -> None:
    candidate = _candidate(
        cumulative=(_cumulative("recovery-1", 2 * EXPECTED_QTY_ATOL, 0.0),),
        incoming=_exact("execution-incoming", EXPECTED_QTY_ATOL, 0.0),
    )

    _assert_refusal(
        prove_execution_coverage_set(candidate),
        ExecutionCoverageSetProofRefusalReason.QUANTITY_MISMATCH,
    )


def test_prove_execution_coverage_set_pins_inclusive_cost_boundary() -> None:
    candidate = _candidate(
        cumulative=(_cumulative("recovery-1", 1.0, 0.0),),
        incoming=_exact("execution-incoming", 1.0, EXPECTED_PRICE_ATOL),
    )

    _assert_success(prove_execution_coverage_set(candidate))

    outside = replace(
        candidate,
        incoming_exact=_exact("execution-incoming", 1.0, math.nextafter(EXPECTED_PRICE_ATOL, math.inf)),
    )
    _assert_refusal(
        prove_execution_coverage_set(outside),
        ExecutionCoverageSetProofRefusalReason.COST_MISMATCH,
    )


def test_prove_execution_coverage_set_refuses_high_price_sub_quantity_tolerance_cost_residue() -> None:
    candidate = _candidate(
        cumulative=(_cumulative("recovery-1", 1.0 + EXPECTED_QTY_ATOL / 2, 1_000_000_000.0),),
        incoming=_exact("execution-incoming", 1.0, 1_000_000_000.0),
    )

    _assert_refusal(
        prove_execution_coverage_set(candidate),
        ExecutionCoverageSetProofRefusalReason.COST_MISMATCH,
    )


def test_prove_execution_coverage_set_refuses_nonfinite_aggregate_from_finite_rows() -> None:
    candidate = _candidate(
        cumulative=(
            _cumulative("recovery-a", 1e308, 1e-308),
            _cumulative("recovery-b", 1e308, 1e-308),
        ),
        incoming=_exact("execution-incoming", 1e308, 1e-308),
    )

    _assert_refusal(
        prove_execution_coverage_set(candidate),
        ExecutionCoverageSetProofRefusalReason.NONFINITE_AGGREGATE,
    )


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        (
            _candidate(cumulative=()),
            ExecutionCoverageSetProofRefusalReason.MISSING_CUMULATIVE_COVERAGE,
        ),
        (
            _candidate(incoming=_exact("", 1.0, 10.0)),
            ExecutionCoverageSetProofRefusalReason.INVALID_SOURCE_IDENTITY,
        ),
        (
            _candidate(prior=(_exact("execution-incoming", 1.0, 10.0),)),
            ExecutionCoverageSetProofRefusalReason.DUPLICATE_SOURCE_ID,
        ),
        (
            _candidate(effective_exact_source_ids=frozenset({"execution-incoming"})),
            ExecutionCoverageSetProofRefusalReason.EXACT_ALREADY_EFFECTIVE,
        ),
        (
            _candidate(active_episode_ids=("episode-1", "episode-2")),
            ExecutionCoverageSetProofRefusalReason.MULTIPLE_ACTIVE_EPISODES,
        ),
        (
            _candidate(unreadable_source_ids=("quarantine-transition-8",)),
            ExecutionCoverageSetProofRefusalReason.UNREADABLE_EVIDENCE,
        ),
        (
            _candidate(cumulative=(_cumulative("recovery-1", 1.0, 10.0, identity=_identity(symbol="QQQ")),)),
            ExecutionCoverageSetProofRefusalReason.IDENTITY_MISMATCH,
        ),
    ],
)
def test_prove_execution_coverage_set_refuses_invalid_identity_and_state(
    candidate: ExecutionCoverageSetCandidate,
    reason: ExecutionCoverageSetProofRefusalReason,
) -> None:
    _assert_refusal(prove_execution_coverage_set(candidate), reason)


@pytest.mark.parametrize(
    ("incoming", "reason"),
    [
        (_exact("execution-incoming", math.nan, 10.0), ExecutionCoverageSetProofRefusalReason.NONFINITE_QUANTITY),
        (_exact("execution-incoming", 0.0, 10.0), ExecutionCoverageSetProofRefusalReason.NONPOSITIVE_QUANTITY),
        (_exact("execution-incoming", -1.0, 10.0), ExecutionCoverageSetProofRefusalReason.NONPOSITIVE_QUANTITY),
        (_exact("execution-incoming", 1.0, math.inf), ExecutionCoverageSetProofRefusalReason.NONFINITE_PRICE),
        (_exact("execution-incoming", 1e308, 1e308), ExecutionCoverageSetProofRefusalReason.NONFINITE_GROSS_COST),
        (_exact("execution-incoming", 1.0, 10.0, fee=math.nan), ExecutionCoverageSetProofRefusalReason.NONFINITE_FEE),
    ],
)
def test_prove_execution_coverage_set_refuses_malformed_economics(
    incoming: ExactCoverageObservation,
    reason: ExecutionCoverageSetProofRefusalReason,
) -> None:
    _assert_refusal(prove_execution_coverage_set(_candidate(incoming=incoming)), reason)


def test_prove_execution_coverage_set_excludes_fees_from_equivalence_and_retains_them_exactly() -> None:
    incoming = _exact("execution-incoming", 1.0, 10.0, fee=0.23)
    candidate = _candidate(incoming=incoming)

    result = _assert_success(prove_execution_coverage_set(candidate))

    assert result.exact.gross_cost == 10.0
    assert result.retained_exact_observations == (incoming,)
    assert result.retained_exact_observations[0].fee == 0.23
