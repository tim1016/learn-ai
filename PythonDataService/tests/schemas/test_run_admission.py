"""Type-boundary tests for ``ProgramBuildAdmissionFact``'s PROVEN invariant.

An independent code-quality review found that ``state == "PROVEN"`` was not
guaranteed at the type level to carry its four proof fields
(``program_version``, ``golden_trace_root``, ``running_artifact_digest``,
``qualification_receipt_hash``) plus non-empty ``evidence_refs`` -- nothing
stopped a caller from constructing a PROVEN fact with some or all of them
absent. ``app/services/bot_binding_repository.py`` had to hand-roll a
``missing_fields`` check for exactly this reason. This file exercises the
``model_validator`` that now closes that gap directly on the schema, so the
invariant is enforced at construction rather than re-derived downstream.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.run_admission import PROGRAM_BUILD_PROOF_FIELDS, ProgramBuildAdmissionFact

_NOW = 1_700_000_010_000


def _proven_kwargs() -> dict[str, object]:
    """A complete, valid PROVEN fact's constructor kwargs."""
    return {
        "state": "PROVEN",
        "program_key": "ema_crossover_signal",
        "program_version": "1",
        "golden_trace_root": "a" * 64,
        "running_artifact_digest": "b" * 64,
        "qualification_receipt_hash": "c" * 64,
        "verified_at_ms": _NOW,
        "evidence_refs": (f"program-build-digest:{'b' * 64}",),
        "explanation": "The running Signal Program build matches its golden qualification receipt.",
    }


def test_proven_fact_with_complete_proof_constructs() -> None:
    fact = ProgramBuildAdmissionFact(**_proven_kwargs())

    assert fact.state == "PROVEN"


@pytest.mark.parametrize("missing_field", PROGRAM_BUILD_PROOF_FIELDS)
def test_proven_fact_missing_any_proof_field_is_rejected(missing_field: str) -> None:
    kwargs = _proven_kwargs()
    kwargs[missing_field] = None

    with pytest.raises(ValidationError, match=missing_field):
        ProgramBuildAdmissionFact(**kwargs)


def test_proven_fact_with_empty_evidence_refs_is_rejected() -> None:
    kwargs = _proven_kwargs()
    kwargs["evidence_refs"] = ()

    with pytest.raises(ValidationError, match="evidence_refs"):
        ProgramBuildAdmissionFact(**kwargs)


@pytest.mark.parametrize("state", ["UNPROVEN", "NOT_APPLICABLE"])
def test_unproven_and_not_applicable_facts_construct_without_any_proof(state: str) -> None:
    """The regression that matters most: today, both real producers
    (``prove_running_program_build``'s early-return branches and
    ``bot_resume_admission._resolve_program_build``'s legacy-clone branch)
    build UNPROVEN/NOT_APPLICABLE facts with every proof field absent and
    ``evidence_refs`` empty. The validator must leave this path alone --
    over-constraining it would refuse admission on a real bot.
    """
    fact = ProgramBuildAdmissionFact(
        state=state,
        program_key="ema_crossover_signal",
        verified_at_ms=_NOW,
        explanation="not proven",
    )

    assert fact.state == state
    assert all(getattr(fact, field) is None for field in PROGRAM_BUILD_PROOF_FIELDS)
    assert fact.evidence_refs == ()
