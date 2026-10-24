"""The StrategySpec research pipeline's file stores, read through the History endpoint (T17).

These write real runs, walk-forwards, Monte Carlos and baselines under a temporary artifacts root with the stores' own
``save_*`` functions, then read them back as the index. The two Postgres-backed stores are stood in for by empty ones.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from app.main import app
from app.research.baselines.result import BaselineConfig, BaselineResult
from app.research.baselines.storage import save_baseline
from app.research.history.contract import HistoryQuery, SourceRead
from app.research.history.registry import default_history_sources
from app.research.monte_carlo.result import MonteCarloConfig, MonteCarloResult
from app.research.monte_carlo.storage import save_monte_carlo
from app.research.runs import BacktestRunResult, RunLedger, RunMetrics, save_run
from app.research.walk_forward import (
    ChronologicalSplitPolicySpec,
    WalkForwardConfig,
    WalkForwardResult,
    save_walk_forward,
)
from app.research.walk_forward.result import FoldResult
from app.routers.research_history import get_history_sources
from app.schemas.research_history import HistoryType

URL = "/api/research/history"
SPY_START, SPY_END = 1_704_171_600_000, 1_735_621_200_000


def hex_id(n: int) -> str:
    return f"{n:032x}"


class EmptyLegacyStore:
    """Stands in for a Postgres-backed store, which holds nothing here."""

    def __init__(self, type_: HistoryType) -> None:
        self.type: HistoryType = type_
        self.label = f"saved {type_} records"

    def order_key(self, native_id: str) -> int:
        return int(native_id)

    async def read(self, query: HistoryQuery, take: int) -> SourceRead:
        return SourceRead(total=0, entries=(), symbols=frozenset())


def a_run(n: int, symbol: str, created_at_ms: int, *, status: str = "completed", parent: str | None = None, **changes: Any) -> RunLedger:
    return RunLedger(
        run_id=hex_id(n),
        parent_run_id=parent,
        strategy_spec_id=f"spec_{n}",
        strategy_spec_hash=f"{n:x}" * 64,
        strategy_spec_json={"name": f"spec {n}"},
        engine_git_commit="test",
        symbol=symbol,
        resolution_minutes=15,
        start_ms=SPY_START,
        end_ms=SPY_END,
        initial_cash=100_000.0,
        fill_mode="signal_bar_close",
        commission_per_order=0.0,
        data_snapshot_id=f"{symbol}|15|{SPY_START}|{SPY_END}|files:abc{n}",
        created_at_ms=created_at_ms,
        completed_at_ms=created_at_ms + 5_000,
        status=status,
        failure_reason="no bars in the window" if status == "failed" else None,
        **changes,
    )


def save_a_run(root: Path, ledger: RunLedger) -> None:
    metrics = RunMetrics(total_trades=0, winning_trades=0, losing_trades=0, total_return_pct=0.0)
    save_run(ledger, BacktestRunResult(run_id=ledger.run_id, initial_cash=100_000.0, final_equity=100_000.0, metrics=metrics), root=root)


def a_fold(index: int, status: str) -> FoldResult:
    metrics = RunMetrics(total_trades=0, winning_trades=0, losing_trades=0, total_return_pct=0.0)
    return FoldResult(
        fold_index=index,
        train_start_ms=1,
        train_end_ms=2,
        test_start_ms=3,
        test_end_ms=4,
        test_run_id=None,
        test_metrics=metrics,
        test_trade_count=0,
        status=status,
        failure_reason="engine failed" if status == "failed" else None,
    )


def save_a_walk_forward(
    root: Path, n: int, parent: str | None, created_at_ms: int, *, status: str = "completed", folds: tuple[str, ...] = ()
) -> None:
    split = ChronologicalSplitPolicySpec(kind="chronological", train_pct=0.7)
    config = WalkForwardConfig(
        walk_forward_id=hex_id(n),
        parent_run_id=parent,
        strategy_spec_hash=f"{n:x}" * 64,
        strategy_spec_json={"name": f"wf spec {n}"},
        symbol="SPY",
        resolution_minutes=15,
        start_ms=SPY_START,
        end_ms=SPY_END,
        initial_cash=100_000.0,
        fill_mode="signal_bar_close",
        commission_per_order=0.0,
        slippage_per_share=0.0,
        random_seed=7,
        split_policy=split,
        created_at_ms=created_at_ms,
    )
    result = WalkForwardResult(
        walk_forward_id=hex_id(n),
        parent_run_id=parent,
        strategy_spec_hash=f"{n:x}" * 64,
        split_policy=split,
        folds=[a_fold(i, s) for i, s in enumerate(folds)],
        created_at_ms=created_at_ms,
        completed_at_ms=created_at_ms + 9_000,
        status=status,
        failure_reason="the split left no test data" if status == "failed" else None,
    )
    save_walk_forward(config, result, root=root)


def save_a_monte_carlo(root: Path, n: int, parent: str, created_at_ms: int) -> None:
    config = MonteCarloConfig(
        monte_carlo_id=hex_id(n),
        parent_run_id=parent,
        parent_trade_log_hash="t" * 64,
        method="reshuffle",
        simulation_count=1000,
        projection_trade_count=0,
        initial_equity=100_000.0,
        random_seed=3,
        created_at_ms=created_at_ms,
    )
    result = MonteCarloResult(
        monte_carlo_id=hex_id(n),
        parent_run_id=parent,
        method="reshuffle",
        simulation_count=1000,
        realised_trade_count=25,
        created_at_ms=created_at_ms,
        completed_at_ms=created_at_ms + 2_000,
        status="completed",
    )
    save_monte_carlo(config, result, root=root)


def save_a_baseline(root: Path, n: int, parent: str, created_at_ms: int, *, status: str) -> None:
    config = BaselineConfig(
        baseline_id=hex_id(n),
        parent_run_id=parent,
        parent_trade_log_hash="t" * 64,
        method="buy_and_hold",
        sample_count=1,
        random_seed=0,
        created_at_ms=created_at_ms,
    )
    result = BaselineResult(
        baseline_id=hex_id(n),
        parent_run_id=parent,
        method="buy_and_hold",
        sample_count=1,
        created_at_ms=created_at_ms,
        completed_at_ms=created_at_ms + 1_000,
        status=status,
        failure_reason="the parent has no trades" if status == "failed" else None,
    )
    save_baseline(config, result, root=root)


RUN_SPY, RUN_QQQ, RUN_FOLD = hex_id(1), hex_id(2), hex_id(3)
WF_OK, WF_PARTIAL, WF_FAILED = hex_id(11), hex_id(12), hex_id(13)
MC_OK, MC_UNREADABLE, MC_ORPHAN = hex_id(21), hex_id(22), hex_id(23)
BASELINE_FAILED = hex_id(31)
CORRUPT_LEDGER = hex_id(99)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    save_a_run(tmp_path, a_run(1, "SPY", 1_700_000_300_000))
    save_a_run(tmp_path, a_run(2, "QQQ", 1_700_000_200_000, status="failed"))
    # A fold's own ledger: its walk-forward's lineage, not a History entry.
    save_a_run(tmp_path, a_run(3, "SPY", 1_700_000_250_000, parent=WF_PARTIAL))
    save_a_walk_forward(tmp_path, 11, RUN_SPY, 1_700_000_400_000, folds=("completed", "completed"))
    save_a_walk_forward(tmp_path, 12, RUN_SPY, 1_700_000_410_000, folds=("completed", "failed", "completed"))
    save_a_walk_forward(tmp_path, 13, None, 1_700_000_420_000, status="failed")
    save_a_monte_carlo(tmp_path, 21, RUN_SPY, 1_700_000_500_000)
    save_a_monte_carlo(tmp_path, 22, RUN_QQQ, 1_700_000_510_000)
    (tmp_path / "monte-carlo" / MC_UNREADABLE / "result.json").write_text("{ not json", encoding="utf-8")
    save_a_monte_carlo(tmp_path, 23, hex_id(404), 1_700_000_520_000)
    save_a_baseline(tmp_path, 31, RUN_QQQ, 1_700_000_600_000, status="failed")
    (tmp_path / CORRUPT_LEDGER).mkdir()
    (tmp_path / CORRUPT_LEDGER / "ledger.json").write_text("{ not json", encoding="utf-8")
    return tmp_path


@pytest.fixture
def stores(root: Path) -> Iterator[None]:
    files = default_history_sources(root)[:4]
    app.dependency_overrides[get_history_sources] = lambda: [*files, EmptyLegacyStore("signal-report"), EmptyLegacyStore("feature-experiment")]
    yield
    app.dependency_overrides.pop(get_history_sources, None)


@pytest.fixture
async def client(stores: None) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


def by_id(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["native_id"]: entry for entry in body["entries"]}


async def test_every_store_is_indexed_with_its_own_status_and_the_one_unparseable_ledger_is_counted(client: httpx.AsyncClient) -> None:
    body = (await client.get(URL, params={"page_size": 100})).json()

    entries = by_id(body)
    assert set(entries) == {RUN_SPY, RUN_QQQ, WF_OK, WF_PARTIAL, WF_FAILED, MC_OK, MC_UNREADABLE, MC_ORPHAN, BASELINE_FAILED}
    assert RUN_FOLD not in entries, "a fold's ledger is lineage, not a History entry"
    assert body["total"] == 9
    assert body["partial"] is True
    sources = {s["type"]: s for s in body["sources"]}
    assert sources["spec-run"]["unreadable"] == 1 and sources["spec-run"]["total"] == 2
    assert all(s["unreadable"] == 0 for t, s in sources.items() if t != "spec-run")
    assert {(e["type"], e["status"]) for e in entries.values()} == {
        ("spec-run", "completed"),
        ("spec-run", "failed"),
        ("walk-forward", "completed"),
        ("walk-forward", "partial"),
        ("walk-forward", "failed"),
        ("monte-carlo", "completed"),
        ("monte-carlo", "unreadable"),
        ("baseline", "failed"),
    }


async def test_a_spec_run_carries_what_its_ledger_records(client: httpx.AsyncClient) -> None:
    entry = by_id((await client.get(URL, params={"page_size": 100})).json())[RUN_SPY]

    assert entry["type"] == "spec-run"
    assert entry["question"] == 'How did "spec_1" trade SPY?'
    assert entry["symbols"] == ["SPY"]
    assert entry["window"] == {"start_ms": SPY_START, "end_ms": SPY_END}
    assert entry["created_at_ms"] == 1_700_000_300_000
    assert entry["completed_at_ms"] == 1_700_000_305_000
    assert entry["provenance"] == f"data SPY|15|{SPY_START}|{SPY_END}|files:abc1; engine 0.1.0"
    assert entry["parent"] is None
    assert entry["status_detail"] is None


async def test_a_failed_run_keeps_its_reason(client: httpx.AsyncClient) -> None:
    entry = by_id((await client.get(URL, params={"status": "failed", "type": "spec-run"})).json())[RUN_QQQ]

    assert entry["status"] == "failed"
    assert entry["status_detail"] == "no bars in the window"


async def test_a_walk_forward_with_failed_folds_is_partial_and_says_how_much(client: httpx.AsyncClient) -> None:
    entries = by_id((await client.get(URL, params={"type": "walk-forward"})).json())

    assert entries[WF_OK]["status"] == "completed" and entries[WF_OK]["status_detail"] is None
    assert entries[WF_PARTIAL]["status"] == "partial"
    assert entries[WF_PARTIAL]["status_detail"] == "1 of 3 folds failed"
    assert entries[WF_FAILED]["status"] == "failed"
    assert entries[WF_FAILED]["status_detail"] == "the split left no test data"
    assert entries[WF_PARTIAL]["parent"] == {"type": "spec-run", "native_id": RUN_SPY}
    assert entries[WF_FAILED]["parent"] is None


async def test_a_study_without_its_own_instrument_reads_it_from_its_parent_run_and_not_recorded_when_that_is_gone(
    client: httpx.AsyncClient,
) -> None:
    entries = by_id((await client.get(URL, params={"page_size": 100})).json())

    assert entries[MC_OK]["symbols"] == ["SPY"]
    assert entries[MC_OK]["window"] == {"start_ms": SPY_START, "end_ms": SPY_END}
    assert entries[MC_OK]["parent"] == {"type": "spec-run", "native_id": RUN_SPY}
    assert entries[BASELINE_FAILED]["symbols"] == ["QQQ"]
    assert entries[MC_OK]["question"] == "What range of results do run 00000000's trades allow? (reshuffle, 1,000 paths)"
    assert entries[BASELINE_FAILED]["question"] == "How does run 00000000 compare with a buy and hold baseline? (1 sample)"
    assert entries[MC_ORPHAN]["symbols"] == [] and entries[MC_ORPHAN]["window"] is None
    assert entries[MC_ORPHAN]["parent"] == {"type": "spec-run", "native_id": hex_id(404)}


async def test_a_study_whose_result_cannot_be_read_is_listed_as_unreadable_not_as_any_outcome(client: httpx.AsyncClient) -> None:
    entry = by_id((await client.get(URL, params={"type": "monte-carlo"})).json())[MC_UNREADABLE]

    assert entry["status"] == "unreadable"
    assert entry["status_detail"] == "its result file cannot be read"
    assert entry["completed_at_ms"] is None


async def test_the_symbol_filter_reaches_studies_through_their_parent_run(client: httpx.AsyncClient) -> None:
    body = (await client.get(URL, params={"symbol": "qqq", "page_size": 100})).json()

    assert set(by_id(body)) == {RUN_QQQ, MC_UNREADABLE, BASELINE_FAILED}
    assert body["total"] == 3
    assert body["symbols"] == ["QQQ", "SPY"]


async def test_a_run_posted_with_a_lowercase_symbol_is_found_by_the_symbol_the_page_offers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spec is not normalised, so a run posted as "spy" is stored that way; the History must still match it."""
    save_a_run(tmp_path, a_run(5, "spy", 1_700_000_300_000))
    save_a_walk_forward(tmp_path, 15, None, 1_700_000_400_000)
    files = default_history_sources(tmp_path)[:4]
    sources = [*files, EmptyLegacyStore("signal-report"), EmptyLegacyStore("feature-experiment")]
    monkeypatch.setitem(app.dependency_overrides, get_history_sources, lambda: sources)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        offered = (await http.get(URL)).json()["symbols"]
        body = (await http.get(URL, params={"symbol": "spy"})).json()

    assert offered == ["SPY"]
    assert set(by_id(body)) == {hex_id(5), hex_id(15)}
    assert by_id(body)[hex_id(5)]["symbols"] == ["SPY"]


async def test_the_status_filter_counts_and_pages_by_each_studys_own_result(client: httpx.AsyncClient) -> None:
    partial = (await client.get(URL, params={"status": "partial"})).json()
    failed = (await client.get(URL, params={"status": "failed", "page_size": 2, "page": 2})).json()

    assert set(by_id(partial)) == {WF_PARTIAL} and partial["total"] == 1
    assert failed["total"] == 3, "the failed spec run, the failed walk-forward and the failed baseline"
    assert len(failed["entries"]) == 1


async def test_the_pages_follow_the_instants_across_the_four_stores(client: httpx.AsyncClient) -> None:
    body = (await client.get(URL, params={"page_size": 100})).json()

    assert [e["created_at_ms"] for e in body["entries"]] == sorted((e["created_at_ms"] for e in body["entries"]), reverse=True)


async def test_reading_the_history_writes_nothing_to_any_store(client: httpx.AsyncClient, root: Path) -> None:
    def stamp() -> dict[str, tuple[int, int]]:
        return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in sorted(root.rglob("*")) if p.is_file()}

    before = stamp()
    for params in ({}, {"status": "partial"}, {"symbol": "SPY", "page": 2, "page_size": 3}):
        assert (await client.get(URL, params=params)).status_code == 200

    assert stamp() == before


async def test_an_artifacts_root_that_does_not_exist_yet_is_an_empty_history_not_a_failure(tmp_path: Path) -> None:
    sources = [*default_history_sources(tmp_path / "never-written")[:4], EmptyLegacyStore("signal-report"), EmptyLegacyStore("feature-experiment")]
    app.dependency_overrides[get_history_sources] = lambda: sources
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            body = (await http.get(URL)).json()
    finally:
        app.dependency_overrides.pop(get_history_sources, None)

    assert body["total"] == 0 and body["entries"] == []
    assert body["partial"] is False
    assert {s["state"] for s in body["sources"]} == {"read"}


async def test_a_store_whose_directory_cannot_be_listed_is_unavailable_not_empty(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self: Path) -> Any:
        raise PermissionError("denied")

    sources = [*default_history_sources(root)[:4], EmptyLegacyStore("signal-report"), EmptyLegacyStore("feature-experiment")]
    app.dependency_overrides[get_history_sources] = lambda: sources
    monkeypatch.setattr(Path, "iterdir", refuse)
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            response = await http.get(URL)
    finally:
        app.dependency_overrides.pop(get_history_sources, None)

    body = response.json()
    assert response.status_code == 200, response.text
    assert body["partial"] is True
    states = {s["type"]: s["state"] for s in body["sources"]}
    assert states == {
        "spec-run": "unavailable",
        "walk-forward": "unavailable",
        "monte-carlo": "unavailable",
        "baseline": "unavailable",
        "signal-report": "read",
        "feature-experiment": "read",
    }
    assert all("its files could not be read" in s["error"] for s in body["sources"] if s["state"] == "unavailable")
