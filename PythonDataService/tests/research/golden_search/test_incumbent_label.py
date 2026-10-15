"""A plan's incumbent label is checked against the server's records at lock, and its seed against the stored default (#3222).

No database: the research store is a stand-in holding one qualification that is also the symbol's default. A lock
that is refused is refused before any snapshot work, so the refusals are read straight from ``service.lock_study``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.research.golden_search import qualifications, service
from app.research.golden_search.models import GoldenSearchRefusal, StudyRow
from app.research.golden_search.planning import protocol_from_request, registry_incumbent
from app.research.golden_search.qualifications import GoldenDefault, QualificationRow, params_sha256
from tests._helpers.golden_search_study import plan_request

PROGRAM = "ema_crossover_signal"
SYMBOL = "SPY"
CONTRACT = _STRATEGY_REGISTRY[PROGRAM].signal_program_contract
assert CONTRACT is not None
REGISTRY = registry_incumbent(PROGRAM, SYMBOL).params
TUNED = {**REGISTRY, "gap": 0.35}


def _qualified(*, symbol: str = SYMBOL, params: dict[str, Any] | None = None) -> QualificationRow:
    point = {**TUNED, "symbol": symbol} if params is None else params
    return QualificationRow(
        id="q-approved-0001", program_key=PROGRAM, program_version=CONTRACT.program_version,
        parameter_schema_version=CONTRACT.parameter_schema_version, symbol=symbol, params=dict(point),
        params_sha256=params_sha256(point), artifact_digest="a" * 64, wiring_digest="w" * 64, study_id="study-1",
        golden_run_id=1, golden_review_id=1, proof={}, proof_sha256="p" * 64, research={}, note="Approved.",
        approved_by="local:owner", created_at_ms=1_000,
    )


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> QualificationRow:
    """The one stored qualification, which is also the default; the lock path never reaches the study tables."""
    row = _qualified()

    @asynccontextmanager
    async def connection() -> AsyncIterator[Any]:
        yield object()

    async def get_default(conn: Any, program_key: str, symbol: str) -> GoldenDefault:
        return GoldenDefault(program_key=program_key, symbol=symbol, qualification_id=row.id, revision=1, updated_at_ms=1_000)

    async def get_qualification(conn: Any, qualification_id: str) -> QualificationRow | None:
        return row if qualification_id == row.id else None

    async def find_qualifications(conn: Any, *, program_key: str, symbol: str, params_sha256: str, program_version: str | None = None) -> list[QualificationRow]:
        return [row] if (program_key, symbol, params_sha256) == (row.program_key, row.symbol, row.params_sha256) else []

    async def not_locked_yet(key: str, digest: str) -> None:
        return None

    monkeypatch.setattr(service, "connection", connection)
    monkeypatch.setattr(qualifications, "get_default", get_default)
    monkeypatch.setattr(qualifications, "get_qualification", get_qualification)
    monkeypatch.setattr(qualifications, "find_qualifications", find_qualifications)
    monkeypatch.setattr(service, "_locked_by", not_locked_yet)
    return row


def _incumbent(source: str, qualification_id: str | None, params: dict[str, Any]) -> dict[str, Any]:
    return {"source": source, "qualification_id": qualification_id, "params": params}


async def _refusal_of(request: dict[str, Any]) -> GoldenSearchRefusal:
    with pytest.raises(GoldenSearchRefusal) as refused:
        await service.lock_study(request, idempotency_key="lock-1", roots=[])
    return refused.value


async def test_a_qualification_that_does_not_exist_cannot_be_the_incumbent(store: QualificationRow) -> None:
    request = plan_request(SYMBOL, incumbent=_incumbent("qualification", "gq-does-not-exist", TUNED))

    refused = await _refusal_of(request)

    assert (refused.code, refused.field) == ("INCUMBENT_INVALID", "incumbent.qualification_id")


async def test_a_qualification_of_another_stock_cannot_be_the_incumbent(store: QualificationRow, monkeypatch: pytest.MonkeyPatch) -> None:
    other = _qualified(symbol="QQQ")

    async def get_qualification(conn: Any, qualification_id: str) -> QualificationRow:
        return other

    monkeypatch.setattr(qualifications, "get_qualification", get_qualification)

    refused = await _refusal_of(plan_request(SYMBOL, incumbent=_incumbent("qualification", other.id, TUNED)))

    assert (refused.code, refused.field) == ("INCUMBENT_INVALID", "incumbent.qualification_id")


async def test_a_qualification_label_over_other_settings_is_refused(store: QualificationRow) -> None:
    request = plan_request(SYMBOL, incumbent=_incumbent("qualification", store.id, {**TUNED, "gap": 0.5}))

    refused = await _refusal_of(request)

    assert (refused.code, refused.field) == ("INCUMBENT_INVALID", "incumbent.params")


async def test_a_registry_label_over_the_qualified_tuple_cannot_dodge_the_seed_gate(store: QualificationRow) -> None:
    # Relabelled "registry", the qualified tuple would otherwise pass the leakage gate that only reads a qualification label.
    request = plan_request(SYMBOL, incumbent=_incumbent("registry", None, TUNED), seed=TUNED)

    refused = await _refusal_of(request)

    assert (refused.code, refused.field) == ("INCUMBENT_INVALID", "incumbent.params")
    assert [item["code"] for item in refused.refusals] == ["INCUMBENT_INVALID", "SEED_IS_QUALIFIED"]


async def test_a_seed_that_is_the_stored_default_tuple_is_refused_whatever_the_incumbent_is_called(store: QualificationRow) -> None:
    request = plan_request(SYMBOL, incumbent=_incumbent("registry", None, REGISTRY), seed=TUNED)

    refused = await _refusal_of(request)

    assert (refused.code, refused.field) == ("SEED_IS_QUALIFIED", "seed")


async def test_the_incumbent_the_defaults_echo_passes_the_check(store: QualificationRow) -> None:
    # Both echoes a client can hold: the default qualification, and the registry point when no default is ready.
    for echoed in (_incumbent("qualification", store.id, store.params), _incumbent("registry", None, REGISTRY)):
        protocol = protocol_from_request(plan_request(SYMBOL, incumbent=echoed, seed=None))

        await service._require_verified_incumbent(protocol)

        assert protocol.seed == REGISTRY  # a qualified incumbent starts its searches at the registry point


@pytest.mark.parametrize(
    ("incumbent", "field"),
    [
        (_incumbent("qualification", "gq-does-not-exist", TUNED), "incumbent.qualification_id"),
        (_incumbent("registry", None, TUNED), "incumbent.params"),
    ],
    ids=["unknown-qualification", "registry-label-over-the-qualified-tuple"],
)
async def test_a_revision_refuses_a_relabelled_incumbent_before_it_forks(
    store: QualificationRow, monkeypatch: pytest.MonkeyPatch, incumbent: dict[str, Any], field: str
) -> None:
    # The revise command reads its plan through the same lock checks: the page's revise form sends the old study's frozen incumbent.
    study = StudyRow(
        id="s1", parent_study_id=None, strategy_key=PROGRAM, symbol=SYMBOL, state="locked", revision=1, status="idle",
        attempt=0, job_id=None, pending_stage=None, stage_token=None, created_at_ms=0, updated_at_ms=0, finished_at_ms=None,
        protocol=protocol_from_request(plan_request(SYMBOL)).as_dict(), protocol_hash="h", receipt={}, results={},
        candidate_key=None, exam_locked=False, decision=None, budget_cap=5000, consumed_evaluations=0, cache_hits=0,
        invalid_points=0, incomplete=False, failure_reason=None, hidden=False,
    )

    async def not_replayed(study_id: str, key: str, digest: str) -> None:
        return None

    async def get_row(study_id: str) -> StudyRow:
        return study

    monkeypatch.setattr(service, "_replayed", not_replayed)
    monkeypatch.setattr(service, "get_row", get_row)
    monkeypatch.setattr(service, "_present", lambda row, **_kwargs: SimpleNamespace(refusals={"revise": None}))

    with pytest.raises(GoldenSearchRefusal) as refused:
        await service.run_command(
            study.id, command="revise", expected_revision=1, idempotency_key="revise-1",
            payload={"protocol": plan_request(SYMBOL, incumbent=incumbent)}, roots=[],
        )

    assert (refused.value.code, refused.value.field) == ("INCUMBENT_INVALID", field)
