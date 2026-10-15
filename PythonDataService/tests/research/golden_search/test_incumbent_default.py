"""The plan page's incumbent: a Golden Search default reads ready only for a build this process can name (#3222).

No database: the research store is a stand-in returning one stored default. The digest judgement is the real
one — the anchor the root conftest primes, and the drift check Deploy and approval already use.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.research.golden_search import qualifications, service
from app.research.golden_search.qualifications import GoldenDefault, QualificationRow, params_sha256
from app.services import signal_program_admission as admission

PROGRAM = "ema_crossover_signal"
SYMBOL = "SPY"
CONTRACT = _STRATEGY_REGISTRY[PROGRAM].signal_program_contract
assert CONTRACT is not None
TUNED = {**CONTRACT.validated_settings, "rsi_min": 30.0, "symbol": SYMBOL}


def _approved_on(artifact_digest: str) -> QualificationRow:
    return QualificationRow(
        id="q-approved-0001", program_key=PROGRAM, program_version=CONTRACT.program_version,
        parameter_schema_version=CONTRACT.parameter_schema_version, symbol=SYMBOL, params=dict(TUNED),
        params_sha256=params_sha256(TUNED), artifact_digest=artifact_digest, wiring_digest="w" * 64, study_id="study-1",
        golden_run_id=1, golden_review_id=1, proof={}, proof_sha256="p" * 64, research={}, note="Approved.",
        approved_by="local:owner", created_at_ms=1_000,
    )


def _store_holds(monkeypatch: pytest.MonkeyPatch, row: QualificationRow) -> None:
    @asynccontextmanager
    async def connection() -> AsyncIterator[Any]:
        yield object()

    async def get_default(conn: Any, program_key: str, symbol: str) -> GoldenDefault:
        return GoldenDefault(program_key=program_key, symbol=symbol, qualification_id=row.id, revision=1, updated_at_ms=1_000)

    async def get_qualification(conn: Any, qualification_id: str) -> QualificationRow:
        return row

    async def events_for(conn: Any, qualification_ids: Any) -> dict[str, list[Any]]:
        return {row.id: []}

    monkeypatch.setattr(service, "connection", connection)
    monkeypatch.setattr(qualifications, "get_default", get_default)
    monkeypatch.setattr(qualifications, "get_qualification", get_qualification)
    monkeypatch.setattr(qualifications, "events_for", events_for)


def _on_the_running_build() -> str:
    return admission.running_build_digests(CONTRACT)[0]


async def test_a_default_proven_on_the_running_build_is_the_incumbent(monkeypatch: pytest.MonkeyPatch) -> None:
    _store_holds(monkeypatch, _approved_on(_on_the_running_build()))

    choice = await service.resolve_incumbent(PROGRAM, SYMBOL)

    assert (choice.ref.source, choice.ref.qualification_id) == ("qualification", "q-approved-0001")
    assert choice.label.startswith("Golden configuration")


async def test_a_default_proven_on_another_build_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    _store_holds(monkeypatch, _approved_on("0" * 64))

    choice = await service.resolve_incumbent(PROGRAM, SYMBOL)

    assert (choice.ref.source, choice.ref.qualification_id) == ("registry", None)
    assert choice.label == f"{service.REGISTRY_LABEL} (the Golden Search default is not ready)"


async def test_a_tree_that_moved_after_import_does_not_label_the_default_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default was proven on exactly the bytes now on disk, but disk is not what this process imported.
    row = _approved_on(_on_the_running_build())
    _store_holds(monkeypatch, row)
    monkeypatch.setattr(admission, "imported_source_drift", lambda contract: "artifact")

    choice = await service.resolve_incumbent(PROGRAM, SYMBOL)

    assert (choice.ref.source, choice.ref.qualification_id) == ("registry", None)
    assert "cannot be judged" in choice.label


async def test_an_unreadable_program_source_degrades_to_the_registry_point_instead_of_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    _store_holds(monkeypatch, _approved_on(_on_the_running_build()))

    def unreadable(contract: Any) -> str:
        raise OSError("program source cannot be read")

    monkeypatch.setattr(admission, "running_artifact_digest", unreadable)

    choice = await service.resolve_incumbent(PROGRAM, SYMBOL)

    assert (choice.ref.source, choice.ref.qualification_id) == ("registry", None)
    assert "cannot be judged" in choice.label
