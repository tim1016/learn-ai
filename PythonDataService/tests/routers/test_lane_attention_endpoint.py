"""``GET /api/brokers/alpaca/attention`` — one lane's bell items (#2228).

The lane answers from its own custody ledger: every unresolved uncertainty,
keyed by the stable uncertainty id a bell dedupes and clears by. A lane with
no active runtime answers empty — *unknown* is the coordinator's judgment
when this read cannot be reached, never this route's.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    raise_uncertainty,
    resolve_exit_not_flat_uncertainty,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXIT_NOT_FLAT_REASON_CODE,
)
from app.main import app

ACCOUNT = "PA-BELL-1"
_T0 = 1_788_040_000_000  # a fixed int64 ms UTC


class _Clock:
    """One steppable repo clock so two episodes observe at distinct stamps."""

    def __init__(self) -> None:
        self.now_ms = _T0

    def __call__(self) -> int:
        return self.now_ms


@pytest.fixture(autouse=True)
def _no_runtime() -> Iterator[None]:
    set_active_clerk_runtime(None)
    yield
    set_active_clerk_runtime(None)


def _raise_exit_not_flat(
    repo: ClerkSqliteRepository, *, strategy_instance_id: str, symbol: str
) -> None:
    raise_uncertainty(
        repo,
        strategy_instance_id=strategy_instance_id,
        reason_code=EXIT_NOT_FLAT_REASON_CODE,
        headline="This bot's exit has not flattened its position",
        explanation="The exit's attributed position is not flat.",
        operator_impact="New exposure for this bot is blocked.",
        next_step="Open the bot's flatten ticket.",
        cause_facts={"symbol": symbol, "attributed_qty": 10.0},
        severity="blocking",
    )


def _repo_with_two_episodes(
    tmp_path: Path,
) -> tuple[ClerkSqliteRepository, _Clock]:
    clock = _Clock()
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT, artifacts_root=tmp_path, clock=clock
    )
    for strategy_instance_id, symbol in (("ema-1", "SPY"), ("ema-2", "QQQ")):
        repo.register_strategy_instance(
            strategy_instance_id=strategy_instance_id, symbol=symbol, config_hash="h1"
        )
        _raise_exit_not_flat(
            repo, strategy_instance_id=strategy_instance_id, symbol=symbol
        )
        clock.now_ms += 1_000
    return repo, clock


async def test_a_lane_with_no_runtime_answers_empty_never_unknown() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/alpaca/attention")

    assert response.status_code == 200
    assert response.json() == {"account_id": None, "items": []}


async def test_items_expose_the_uncertainty_id_symbol_and_order(
    tmp_path: Path,
) -> None:
    repo, _clock = _repo_with_two_episodes(tmp_path)
    set_active_clerk_runtime(
        ActiveClerkRuntime(
            authority_kind="sqlite", _sqlite_repository=repo, account_id=ACCOUNT
        )
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/alpaca/attention")

    assert response.status_code == 200
    body = response.json()
    assert body["account_id"] == ACCOUNT
    items = body["items"]
    assert [item["strategy_instance_id"] for item in items] == ["ema-1", "ema-2"], (
        "oldest observation first — the bell lists what has been waiting longest on top"
    )
    first, second = items
    # The bell's stable identity is the ledger's uncertainty id, so an item
    # clears exactly when its episode resolves.
    assert first["condition_id"] == repo.active_uncertainties()[0]["uncertainty_id"]
    assert first["reason_code"] == EXIT_NOT_FLAT_REASON_CODE
    assert first["kind"] == "uncertainty"
    assert first["severity"] == "blocking"
    assert first["symbol"] == "SPY"
    assert first["headline"] == "This bot's exit has not flattened its position"
    assert second["symbol"] == "QQQ"


async def test_an_item_clears_exactly_when_its_episode_resolves(
    tmp_path: Path,
) -> None:
    repo, _clock = _repo_with_two_episodes(tmp_path)
    set_active_clerk_runtime(
        ActiveClerkRuntime(
            authority_kind="sqlite", _sqlite_repository=repo, account_id=ACCOUNT
        )
    )

    assert resolve_exit_not_flat_uncertainty(
        repo, strategy_instance_id="ema-1", evidence_refs=("order:o1",)
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/alpaca/attention")

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["strategy_instance_id"] for item in items] == ["ema-2"]


async def test_other_brokers_are_404() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/brokers/ibkr/attention")

    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "lane_attention_unsupported_broker"
