"""The server-held incumbent and qualified-seed checks shared by preflight and lock (#3276)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from app.research.golden_search import qualifications, service
from app.research.golden_search.planning import protocol_from_request
from app.research.golden_search.protocol import IncumbentRef
from tests._helpers.golden_search_study import plan_request


@asynccontextmanager
async def _connection():
    yield SimpleNamespace()


async def test_a_registry_label_cannot_hide_a_non_default_qualified_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    protocol = protocol_from_request(plan_request("SPY"))
    qualified = {**protocol.seed, "gap": 0.35}
    protocol = replace(protocol, seed=qualified)

    async def no_default(_conn: Any, *_subject: str) -> None:
        return None

    async def found(_conn: Any, **_kwargs: Any) -> list[Any]:
        return [SimpleNamespace(id="q-older", params=qualified)]

    monkeypatch.setattr(service, "connection", _connection)
    monkeypatch.setattr(qualifications, "get_default", no_default)
    monkeypatch.setattr(qualifications, "find_qualifications", found)

    refusals = await service._verified_incumbent_refusals(protocol)
    assert [refusal.code for refusal in refusals] == ["SEED_IS_QUALIFIED"]


async def test_revision_can_keep_its_frozen_registry_incumbent_after_registry_moves(monkeypatch: pytest.MonkeyPatch) -> None:
    protocol = protocol_from_request(plan_request("SPY"))
    old = protocol.incumbent.params
    moved = {**old, "gap": 0.35}

    async def no_default(_conn: Any, *_subject: str) -> None:
        return None

    async def none_found(_conn: Any, **_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(service, "connection", _connection)
    monkeypatch.setattr(qualifications, "get_default", no_default)
    monkeypatch.setattr(qualifications, "find_qualifications", none_found)
    monkeypatch.setattr(service, "registry_incumbent", lambda *_subject: IncumbentRef("registry", None, moved))

    assert [refusal.code for refusal in await service._verified_incumbent_refusals(protocol)] == ["INCUMBENT_INVALID"]
    frozen = {"source": "registry", "qualification_id": None, "params": old}
    assert await service._verified_incumbent_refusals(protocol, frozen_incumbent=frozen) == ()
