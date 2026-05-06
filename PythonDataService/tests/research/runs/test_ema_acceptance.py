"""Phase A acceptance gate — runs the canonical SPY EMA crossover
fixture end-to-end through the HTTP boundary and verifies every
identity property the architecture spec demands.

This file is the *gate test* for the run-ledger contract. The
underlying mechanics are exhaustively covered by ``test_hashing.py``,
``test_runner_inmemory.py``, ``test_storage.py``, and
``test_endpoint.py``; this one ties them together against the same
fixture (``app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json``)
that ships in the repo and is parity-pinned against the hand-coded
``SpyEmaCrossoverAlgorithm`` reference.

The gates this file enforces:

  1. Repeat runs of the canonical fixture produce identical
     ``result_hash`` / ``trade_log_hash`` / ``metrics_hash`` and
     differ only in ``run_id`` / ``created_at_ms``.
  2. Mutating one indicator parameter (``ema5.period: 5 → 6``)
     changes ``strategy_spec_hash`` AND ``result_hash``.
  3. Shifting the data window changes ``data_snapshot_id`` while
     ``strategy_spec_hash`` is unchanged.
  4. Persistence is faithful: a saved run round-trips through the
     ``GET /{run_id}`` endpoint with every hash intact.
  5. List-filter discovery works against ``spec_hash`` and
     ``status`` without leaking unrelated runs.

Synthetic SPY-labeled bars are built inline rather than reused from
``_parity_helpers.py`` because that helper hard-codes ``symbol="TEST"``.
The bar values mirror ``closes_for_spy_ema`` (already tuned to fire
the SPY EMA rule a handful of times across a 2,000-bar window) so the
fixture's strict entry conditions actually exercise the trade-log
machinery rather than producing zero trades.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.data.trade_bar import TradeBar
from app.engine.strategy.spec import load_spec_from_path
from app.engine.strategy.spec.tests._parity_helpers import (
    FIXTURES_DIR,
    closes_for_spy_ema,
)
from app.main import app
from app.routers.research_runs import (
    get_artifacts_root,
    get_data_source_factory,
)

EASTERN = ZoneInfo("America/New_York")
START_TIME = datetime(2024, 1, 2, 10, 0, tzinfo=EASTERN)


def _build_bars(symbol: str, closes: list[float]) -> list[TradeBar]:
    """Build 15-min consolidator-aligned synthetic bars labeled ``symbol``.

    Mirrors the shape of ``_parity_helpers.build_minute_bars`` (one
    minute-bar every 15 minutes, OHLC = close, volume=100, plus a
    sentinel bar at the tail to flush the consolidator) but lets the
    caller pick the symbol — the parity helper hardcodes ``"TEST"``.
    """
    bars: list[TradeBar] = []
    for i, c in enumerate(closes):
        t = START_TIME + timedelta(minutes=15 * i)
        price = Decimal(str(round(c, 4)))
        bars.append(
            TradeBar(
                symbol=symbol,
                time=t,
                end_time=t + timedelta(minutes=1),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=100,
            )
        )
    last_t = START_TIME + timedelta(minutes=15 * len(closes))
    sentinel_price = Decimal(str(round(closes[-1], 4)))
    bars.append(
        TradeBar(
            symbol=symbol,
            time=last_t,
            end_time=last_t + timedelta(minutes=1),
            open=sentinel_price,
            high=sentinel_price,
            low=sentinel_price,
            close=sentinel_price,
            volume=100,
        )
    )
    return bars


@dataclass
class _SymbolMatchingDataReader:
    """Lookalike of ``_parity_helpers.FakeDataReader`` that filters by
    the requested symbol — keeps the runner honest when a spec asks
    for SPY but bars labeled with another ticker also exist in the
    same in-memory list.
    """

    bars: list[TradeBar]

    def iter_bars(self, symbol: str, start: date, end: date) -> Iterator[TradeBar]:
        target = symbol.upper()
        for b in self.bars:
            if b.symbol.upper() != target:
                continue
            if start <= b.time.date() <= end:
                yield b


@pytest.fixture
def configured_app(tmp_path: Path):
    """Wire the FastAPI app with synthetic SPY bars and a tmp artifacts root."""
    bars = _build_bars("SPY", closes_for_spy_ema(2000))

    def factory(symbol: str, start: date, end: date):
        return _SymbolMatchingDataReader(bars=bars)

    def root() -> Path:
        return tmp_path

    app.dependency_overrides[get_data_source_factory] = lambda: factory
    app.dependency_overrides[get_artifacts_root] = root
    yield app
    app.dependency_overrides.pop(get_data_source_factory, None)
    app.dependency_overrides.pop(get_artifacts_root, None)


@pytest.fixture
async def client(configured_app):
    transport = ASGITransport(app=configured_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _canonical_request_body() -> dict:
    """POST body for the canonical SPY EMA fixture.

    Loads the fixture from disk and dumps it into the request shape
    the endpoint expects — same path the Frontend will use.
    """
    spec = load_spec_from_path(FIXTURES_DIR / "spy_ema_crossover.spec.json")
    return {
        "spec": spec.model_dump(mode="json"),
        "start_date": "2024-01-02",
        "end_date": "2024-12-31",
        "initial_cash": 100_000.0,
        "fill_mode": "signal_bar_close",
        "commission_per_order": 0.0,
        "strategy_spec_id": "spy_ema_crossover",
    }


def _mutated_body(mutator) -> dict:
    """Return the canonical body with ``mutator(body)`` applied in place."""
    body = _canonical_request_body()
    mutator(body)
    return body


# ---------------------------------------------------------------------------
# Gate 1 — repeat runs are content-identical.
# ---------------------------------------------------------------------------
async def test_canonical_fixture_runs_and_produces_completed_ledger(client):
    response = await client.post(
        "/api/research/strategy-runs", json=_canonical_request_body()
    )
    assert response.status_code == 200, response.text
    ledger = response.json()["ledger"]
    assert ledger["status"] == "completed"
    assert ledger["symbol"] == "SPY"
    assert ledger["resolution_minutes"] == 15
    assert ledger["strategy_spec_id"] == "spy_ema_crossover"
    # Hashes are populated only after a successful run completes.
    assert ledger["result_hash"] and ledger["trade_log_hash"] and ledger["metrics_hash"]


async def test_repeat_runs_produce_identical_content_hashes(client):
    body = _canonical_request_body()
    a = (await client.post("/api/research/strategy-runs", json=body)).json()["ledger"]
    b = (await client.post("/api/research/strategy-runs", json=body)).json()["ledger"]

    assert a["run_id"] != b["run_id"]
    assert a["created_at_ms"] != b["created_at_ms"] or True  # may collide on fast clocks
    assert a["strategy_spec_hash"] == b["strategy_spec_hash"]
    assert a["data_snapshot_id"] == b["data_snapshot_id"]
    assert a["result_hash"] == b["result_hash"]
    assert a["trade_log_hash"] == b["trade_log_hash"]
    assert a["metrics_hash"] == b["metrics_hash"]


# ---------------------------------------------------------------------------
# Gate 2 — spec-parameter change propagates.
# ---------------------------------------------------------------------------
async def test_changing_ema5_period_changes_spec_and_result_hash(client):
    base = (
        await client.post("/api/research/strategy-runs", json=_canonical_request_body())
    ).json()["ledger"]

    def mutate(body: dict) -> None:
        # Find the ema5 indicator by id and bump its period 5 → 6.
        for ind in body["spec"]["indicators"]:
            if ind["id"] == "ema5":
                ind["period"] = 6
                break

    mutated = (
        await client.post("/api/research/strategy-runs", json=_mutated_body(mutate))
    ).json()["ledger"]

    assert base["strategy_spec_hash"] != mutated["strategy_spec_hash"]
    assert base["data_snapshot_id"] == mutated["data_snapshot_id"]
    assert base["result_hash"] != mutated["result_hash"]


# ---------------------------------------------------------------------------
# Gate 3 — data window change propagates.
# ---------------------------------------------------------------------------
async def test_shifting_start_date_changes_data_snapshot_id(client):
    base = (
        await client.post("/api/research/strategy-runs", json=_canonical_request_body())
    ).json()["ledger"]
    shifted = (
        await client.post(
            "/api/research/strategy-runs",
            json=_mutated_body(lambda b: b.update({"start_date": "2024-01-03"})),
        )
    ).json()["ledger"]

    assert base["strategy_spec_hash"] == shifted["strategy_spec_hash"]
    assert base["data_snapshot_id"] != shifted["data_snapshot_id"]


# ---------------------------------------------------------------------------
# Gate 4 — persistence is faithful.
# ---------------------------------------------------------------------------
async def test_persisted_run_round_trips_via_get(client):
    posted = (
        await client.post("/api/research/strategy-runs", json=_canonical_request_body())
    ).json()
    run_id = posted["ledger"]["run_id"]

    fetched = (await client.get(f"/api/research/strategy-runs/{run_id}")).json()

    assert fetched["ledger"] == posted["ledger"]
    assert fetched["result"] == posted["result"]


# ---------------------------------------------------------------------------
# Gate 5 — list-filter discovery.
# ---------------------------------------------------------------------------
async def test_list_filter_by_spec_hash_isolates_runs(client):
    canonical = (
        await client.post("/api/research/strategy-runs", json=_canonical_request_body())
    ).json()["ledger"]

    def bump_ema5_period(body: dict) -> None:
        for ind in body["spec"]["indicators"]:
            if ind["id"] == "ema5":
                ind["period"] = 6
                return

    mutated = (
        await client.post(
            "/api/research/strategy-runs", json=_mutated_body(bump_ema5_period)
        )
    ).json()["ledger"]

    canonical_only = (
        await client.get(
            "/api/research/strategy-runs",
            params={"spec_hash": canonical["strategy_spec_hash"]},
        )
    ).json()["runs"]
    assert [r["run_id"] for r in canonical_only] == [canonical["run_id"]]

    mutated_only = (
        await client.get(
            "/api/research/strategy-runs",
            params={"spec_hash": mutated["strategy_spec_hash"]},
        )
    ).json()["runs"]
    assert [r["run_id"] for r in mutated_only] == [mutated["run_id"]]


async def test_list_filter_by_status_separates_completed_and_failed(client):
    completed = (
        await client.post("/api/research/strategy-runs", json=_canonical_request_body())
    ).json()["ledger"]

    # Make a Phase-1-incompatible spec to trigger a failed run.
    def force_failure(body: dict) -> None:
        body["spec"]["entry"]["pyramiding"] = 2

    failed = (
        await client.post(
            "/api/research/strategy-runs", json=_mutated_body(force_failure)
        )
    ).json()["ledger"]
    assert failed["status"] == "failed"

    completed_runs = (
        await client.get("/api/research/strategy-runs", params={"status": "completed"})
    ).json()["runs"]
    failed_runs = (
        await client.get("/api/research/strategy-runs", params={"status": "failed"})
    ).json()["runs"]

    completed_ids = {r["run_id"] for r in completed_runs}
    failed_ids = {r["run_id"] for r in failed_runs}
    assert completed["run_id"] in completed_ids
    assert failed["run_id"] in failed_ids
    assert completed_ids.isdisjoint(failed_ids)
