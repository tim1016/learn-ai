"""A Golden Search qualification extends corpus coverage to one exact tuple, and nothing else (#2696).

The seal pins the qualification that covered it; the build proof re-verifies
that one id against the running build; the pure admission policy then treats
a qualified tuple exactly like the registry's validated point. Every lookup
here is an injected stand-in for the research store, so these run in PR CI;
the store itself is exercised in ``tests/research/golden_search``.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.research.golden_search.qualifications import (
    Coverage,
    QualificationEvent,
    QualificationEvidence,
    QualificationRow,
    QualificationSubject,
    params_sha256,
)
from app.schemas.run_admission import (
    CORPUS_UNCOVERED_EXPLANATION,
    QUALIFICATION_ABSENT,
    QUALIFICATION_COVERED,
    QUALIFICATION_NOT_REVERIFIED,
    QUALIFICATION_REVOKED,
    QUALIFICATION_STALE,
    QUALIFICATION_UNVERIFIABLE,
    REGISTRY_POINT_COVERED,
    ProgramBuildAdmissionFact,
    StrategyValidationAdmissionFact,
)
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.run_admission import evaluate_run_admission
from app.services.signal_program_admission import (
    build_start_program_seal,
    prove_running_program_build,
    resolve_admission_coverage,
    running_build_digests,
)
from tests.services.test_run_admission import _NOW as _ADMISSION_NOW
from tests.services.test_run_admission import _bot, _clerk

PROGRAM = "ema_crossover_signal"
CONTRACT = _STRATEGY_REGISTRY[PROGRAM].signal_program_contract
assert CONTRACT is not None
_SID = "golden-qualified-1"
_VERIFIED_AT = _ADMISSION_NOW - 1_000
# A configuration the registry's validated point does not cover.
_TUNED = {"gap": 0.15, "gap_bps": 0.0, "rsi_min": 48.0, "rsi_max": 72.0, "fast_period": 8, "slow_period": 21}
_TUNED_ORIGINS = {name: "deploy_override" for name in _TUNED}
_REGISTRY = {"gap": 0.2, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0}


def _running_digest() -> str:
    return running_build_digests(CONTRACT)[0]


def _canonical(symbol: str, values: dict[str, object]) -> dict[str, object]:
    registration = _STRATEGY_REGISTRY[PROGRAM]
    return registration.param_schema.model_validate({**values, "symbol": symbol}).model_dump(mode="json")


def _binding(*, symbol: str = "SPY", params: dict[str, object] | None = None, **updates: object) -> BrokerBotBinding:
    values = dict(_TUNED if params is None else params)
    payload: dict[str, object] = {
        "strategy_instance_id": _SID,
        "strategy_key": PROGRAM,
        "broker": "alpaca",
        "symbol": symbol,
        "use_rth": True,
        "mode": "log_only",
        "quantity": 1,
        "carryover_policy": "FORBID",
        "action_plan": alpaca_v1_action_plan(symbol.upper()),
        "strategy_params": values,
        "strategy_param_origins": {name: "deploy_override" for name in values},
        "sealed_account_id": "paper-account",
        "run_id": "run-1",
        "created_at_ms": _VERIFIED_AT,
    }
    payload.update(updates)
    return BrokerBotBinding.model_validate(payload)


def _validation() -> StrategyValidationAdmissionFact:
    return StrategyValidationAdmissionFact(
        state="VERIFIED",
        strategy_key=PROGRAM,
        evidence_status="accepted",
        event_id="golden-validation:7:review:9",
        evidence_snapshot_sha256="a" * 64,
        verified_at_ms=_VERIFIED_AT,
        explanation="The Golden Validation review is current.",
    )


def _qualification(
    qualification_id: str,
    *,
    symbol: str = "SPY",
    values: dict[str, object] | None = None,
    artifact_digest: str | None = None,
    created_at_ms: int = 1_000,
) -> QualificationRow:
    point = _canonical(symbol, _TUNED if values is None else values)
    return QualificationRow(
        id=qualification_id,
        program_key=PROGRAM,
        program_version=CONTRACT.program_version,
        parameter_schema_version=CONTRACT.parameter_schema_version,
        symbol=symbol,
        params=point,
        params_sha256=params_sha256(point),
        artifact_digest=_running_digest() if artifact_digest is None else artifact_digest,
        wiring_digest="w" * 64,
        study_id=f"study-{qualification_id}",
        golden_run_id=7,
        golden_review_id=9,
        proof={},
        proof_sha256="p" * 64,
        research={"exam_outcome": "meets_rules"},
        note="Approved after the final test.",
        approved_by="local:owner",
        created_at_ms=created_at_ms,
    )


def _revoked(qualification_id: str) -> QualificationEvent:
    return QualificationEvent(
        id=1,
        qualification_id=qualification_id,
        kind="revoked",
        artifact_digest=None,
        wiring_digest=None,
        proof=None,
        reason="Owner withdrew it.",
        actor="local:owner",
        command_id=f"revoke-{qualification_id}",
        created_at_ms=2_000,
    )


class _Store:
    """The research store as admission sees it: every qualification it holds, whatever the subject asked."""

    def __init__(self, *evidence: QualificationEvidence) -> None:
        self.evidence = list(evidence)
        self.subjects: list[QualificationSubject] = []

    async def __call__(self, subject: QualificationSubject) -> Sequence[QualificationEvidence]:
        self.subjects.append(subject)
        return list(self.evidence)


async def _unreadable(subject: QualificationSubject) -> Sequence[QualificationEvidence]:
    raise ConnectionRefusedError("research store is down")


async def _no_read(subject: QualificationSubject) -> Sequence[QualificationEvidence]:
    raise AssertionError("the registry point must not read the research store")


def _ready(qualification_id: str = "gq-1", **kwargs: object) -> QualificationEvidence:
    return QualificationEvidence(qualification=_qualification(qualification_id, **kwargs), events=())


async def _admit(binding: BrokerBotBinding, lookup) -> tuple[BrokerBotBinding, ProgramBuildAdmissionFact]:
    """The Start path: resolve, seal, then re-verify the sealed binding exactly as admission does."""
    coverage = await resolve_admission_coverage(binding, lookup=lookup)
    seal = build_start_program_seal(
        binding, _validation(), parameter_origins=binding.strategy_param_origins, coverage=coverage
    )
    assert seal is not None
    sealed = binding.model_copy(update={"sealed_program": seal})
    proof = prove_running_program_build(sealed, verified_at_ms=_VERIFIED_AT, coverage=coverage)
    return sealed, proof


# ---------------------------------------------------------------------------
# Coverage resolution and the seal
# ---------------------------------------------------------------------------
async def test_resolve_admission_coverage_covers_the_registry_point_without_reading_the_store() -> None:
    coverage = await resolve_admission_coverage(_binding(params=_REGISTRY), lookup=_no_read)

    assert coverage == Coverage(state="COVERED", qualification_id=None, explanation=REGISTRY_POINT_COVERED)


async def test_approved_tuple_is_sealed_with_its_qualification_and_proven_covered() -> None:
    store = _Store(_ready("gq-1"))

    sealed, proof = await _admit(_binding(), store)

    configured = sealed.sealed_program.configured_signal
    assert configured.parameters_match_validated_settings is False
    assert configured.qualification_id == "gq-1"
    assert proof.state == "PROVEN"
    assert proof.corpus_coverage == "COVERED"
    assert QUALIFICATION_COVERED in proof.explanation
    assert CORPUS_UNCOVERED_EXPLANATION not in proof.explanation
    assert proof.next_step is None
    assert "golden-qualification:gq-1" in proof.evidence_refs
    assert "program-corpus-coverage:COVERED" in proof.evidence_refs


async def test_resolve_admission_coverage_asks_for_the_canonical_upper_case_tuple() -> None:
    store = _Store(_ready("gq-1"))

    sealed, proof = await _admit(_binding(symbol="spy"), store)

    expected = _canonical("SPY", _TUNED)
    assert store.subjects[0] == QualificationSubject(
        program_key=PROGRAM,
        program_version=CONTRACT.program_version,
        symbol="SPY",
        params_sha256=params_sha256(expected),
    )
    assert proof.corpus_coverage == "COVERED"
    assert sealed.sealed_program.configured_signal.qualification_id == "gq-1"


async def test_registry_point_is_unaffected_by_qualifications() -> None:
    store = _Store(_ready("gq-1", values=_REGISTRY))

    sealed, proof = await _admit(_binding(params=_REGISTRY), store)

    assert sealed.sealed_program.configured_signal.qualification_id is None
    assert sealed.sealed_program.configured_signal.parameters_match_validated_settings is True
    assert proof.corpus_coverage == "COVERED"
    assert proof.explanation == "The running Signal Program build matches its golden qualification receipt."
    assert not any(ref.startswith("golden-qualification:") for ref in proof.evidence_refs)
    assert store.subjects == []


@pytest.mark.parametrize(
    ("evidence", "explanation"),
    [
        (lambda: _ready("gq-1", artifact_digest="0" * 64), QUALIFICATION_STALE),
        (
            lambda: QualificationEvidence(qualification=_qualification("gq-1"), events=(_revoked("gq-1"),)),
            QUALIFICATION_REVOKED,
        ),
    ],
    ids=["stale", "revoked"],
)
async def test_stale_or_revoked_qualification_seals_nothing_and_proves_uncovered(evidence, explanation: str) -> None:
    sealed, proof = await _admit(_binding(), _Store(evidence()))

    assert sealed.sealed_program.configured_signal.qualification_id is None
    assert proof.state == "PROVEN"
    assert proof.corpus_coverage == "UNCOVERED"
    assert CORPUS_UNCOVERED_EXPLANATION in proof.explanation
    assert not any(ref.startswith("golden-qualification:") for ref in proof.evidence_refs)
    coverage = await resolve_admission_coverage(_binding(), lookup=_Store(evidence()))
    assert coverage is not None and coverage.explanation == explanation


async def test_unreadable_store_fails_closed_with_cannot_verify() -> None:
    coverage = await resolve_admission_coverage(_binding(), lookup=_unreadable)

    assert coverage == Coverage(state="UNCOVERED", qualification_id=None, explanation=QUALIFICATION_UNVERIFIABLE)
    sealed, proof = await _admit(_binding(), _unreadable)
    assert sealed.sealed_program.configured_signal.qualification_id is None
    assert proof.corpus_coverage == "UNCOVERED"


async def test_unrelated_stock_is_unaffected_by_another_stocks_qualification() -> None:
    store = _Store(_ready("gq-spy", symbol="SPY"))

    tuned_qqq, tuned_proof = await _admit(_binding(symbol="QQQ"), store)
    registry_qqq, registry_proof = await _admit(_binding(symbol="QQQ", params=_REGISTRY), store)

    assert tuned_qqq.sealed_program.configured_signal.qualification_id is None
    assert tuned_proof.corpus_coverage == "UNCOVERED"
    coverage = await resolve_admission_coverage(_binding(symbol="QQQ"), lookup=store)
    assert coverage is not None and coverage.explanation == QUALIFICATION_ABSENT
    assert registry_proof.corpus_coverage == "COVERED"
    assert registry_qqq.sealed_program.configured_signal.qualification_id is None


# ---------------------------------------------------------------------------
# Re-verification of a pinned seal
# ---------------------------------------------------------------------------
async def test_pinned_seal_reverifies_only_its_own_qualification() -> None:
    sealed, _proof = await _admit(_binding(), _Store(_ready("gq-1")))
    # Later: the pinned version is revoked and a newer one of the same tuple is approved.
    later = _Store(
        QualificationEvidence(qualification=_qualification("gq-1"), events=(_revoked("gq-1"),)),
        _ready("gq-2", created_at_ms=5_000),
    )

    coverage = await resolve_admission_coverage(sealed, lookup=later)
    proof = prove_running_program_build(sealed, verified_at_ms=_VERIFIED_AT, coverage=coverage)

    assert coverage == Coverage(
        state="UNCOVERED", qualification_id=None, explanation=QUALIFICATION_REVOKED, artifact_digest=_running_digest()
    )
    assert proof.corpus_coverage == "UNCOVERED"
    assert QUALIFICATION_REVOKED in proof.explanation
    assert not any(ref.startswith("golden-qualification:") for ref in proof.evidence_refs)


async def test_pinned_seal_stays_covered_when_a_newer_default_exists() -> None:
    sealed, _proof = await _admit(_binding(), _Store(_ready("gq-1")))
    later = _Store(_ready("gq-1"), _ready("gq-2", created_at_ms=5_000))

    coverage = await resolve_admission_coverage(sealed, lookup=later)
    proof = prove_running_program_build(sealed, verified_at_ms=_VERIFIED_AT, coverage=coverage)

    assert coverage is not None and coverage.qualification_id == "gq-1"
    assert proof.corpus_coverage == "COVERED"
    assert "golden-qualification:gq-1" in proof.evidence_refs


async def test_pinned_seal_without_reverification_is_uncovered() -> None:
    sealed, _proof = await _admit(_binding(), _Store(_ready("gq-1")))

    proof = prove_running_program_build(sealed, verified_at_ms=_VERIFIED_AT)

    assert proof.corpus_coverage == "UNCOVERED"
    assert QUALIFICATION_NOT_REVERIFIED in proof.explanation


async def test_coverage_judged_against_other_bytes_is_not_reused() -> None:
    sealed, _proof = await _admit(_binding(), _Store(_ready("gq-1")))
    elsewhere = Coverage(
        state="COVERED", qualification_id="gq-1", explanation=QUALIFICATION_COVERED, artifact_digest="9" * 64
    )

    proof = prove_running_program_build(sealed, verified_at_ms=_VERIFIED_AT, coverage=elsewhere)

    assert proof.corpus_coverage == "UNCOVERED"
    assert QUALIFICATION_NOT_REVERIFIED in proof.explanation


async def test_pinned_seal_with_an_unreadable_store_says_cannot_verify() -> None:
    sealed, _proof = await _admit(_binding(), _Store(_ready("gq-1")))

    coverage = await resolve_admission_coverage(sealed, lookup=_unreadable)
    proof = prove_running_program_build(sealed, verified_at_ms=_VERIFIED_AT, coverage=coverage)

    assert proof.corpus_coverage == "UNCOVERED"
    assert QUALIFICATION_UNVERIFIABLE in proof.explanation


async def test_seal_that_pins_nothing_needs_no_read() -> None:
    sealed, _proof = await _admit(_binding(), _Store())

    assert await resolve_admission_coverage(sealed, lookup=_no_read) is None


# ---------------------------------------------------------------------------
# The pure admission policy
# ---------------------------------------------------------------------------
def _facts(proof: ProgramBuildAdmissionFact, *, mode: str = "log_only", sealed_account_id: str = "paper-account"):
    return _bot(mode=mode).model_copy(update={"program_build": proof, "sealed_account_id": sealed_account_id})


@pytest.mark.parametrize("account_mode", ["paper", "live"])
async def test_approved_tuple_is_admitted_on_paper_and_live(account_mode: str) -> None:
    _sealed, proof = await _admit(_binding(), _Store(_ready("gq-1")))
    account = f"{account_mode}-account"

    decision = evaluate_run_admission(
        _facts(proof, sealed_account_id=account),
        _clerk(account_id=account, account_mode=account_mode),
        evaluated_at_ms=_ADMISSION_NOW,
    )

    assert decision.allowed is True
    assert decision.reason_code == "START_ADMITTED"
    assert "golden-qualification:gq-1" in decision.evidence_refs


@pytest.mark.parametrize(
    "lookup",
    [
        lambda: _Store(_ready("gq-1", artifact_digest="0" * 64)),
        lambda: _Store(QualificationEvidence(qualification=_qualification("gq-1"), events=(_revoked("gq-1"),))),
        lambda: _unreadable,
    ],
    ids=["stale", "revoked", "unreadable"],
)
async def test_uncovered_tuple_is_refused_outside_dry_run_and_admitted_in_dry_run(lookup) -> None:
    _sealed, proof = await _admit(_binding(), lookup())

    refused = evaluate_run_admission(_facts(proof), _clerk(), evaluated_at_ms=_ADMISSION_NOW)
    exploring = evaluate_run_admission(_facts(proof, mode="dry_run"), _clerk(), evaluated_at_ms=_ADMISSION_NOW)

    assert refused.allowed is False
    assert refused.reason_code == "PROGRAM_CORPUS_UNCOVERED"
    assert exploring.allowed is True


async def test_evidence_override_cannot_bypass_an_unproven_build_of_a_qualified_tuple() -> None:
    """A qualification covers parameters; it never stands in for proven bytes, override or not."""
    store = _Store(_ready("gq-1"))
    binding = _binding(
        evidence_override={
            "acknowledgement": "I_ACCEPT_EVIDENCE_ONLY_DEPLOYMENT_RISK",
            "reason": "Owner accepts the research weakness.",
        }
    )
    coverage = await resolve_admission_coverage(binding, lookup=store)
    seal = build_start_program_seal(
        binding, _validation(), parameter_origins=binding.strategy_param_origins, coverage=coverage
    )
    assert seal is not None and seal.configured_signal.qualification_id == "gq-1"
    # The sealed program has since moved: its golden trace root no longer matches the registry.
    moved = seal.configured_signal.model_copy(update={"golden_trace_root": "f" * 64})
    tampered = binding.model_copy(update={"sealed_program": seal.model_copy(update={"configured_signal": moved})})

    proof = prove_running_program_build(tampered, verified_at_ms=_VERIFIED_AT, coverage=coverage)
    decision = evaluate_run_admission(_facts(proof), _clerk(), evaluated_at_ms=_ADMISSION_NOW)

    assert proof.state == "UNPROVEN"
    assert decision.allowed is False
    assert decision.reason_code == "PROGRAM_BUILD_UNPROVEN"
