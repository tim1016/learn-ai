"""HTTP seam for custom Dark Bright Gates saved on a strategy (#2639 D8–D10)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.routers import strategy_gates
from app.services.strategy_gate_store import StrategyGateStore

_EMA = "/api/strategy-gates/ema_crossover_signal"


@pytest.fixture
def client(tmp_path: Path) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(strategy_gates.router, prefix="/api/strategy-gates")
    app.dependency_overrides[strategy_gates.get_gate_store] = lambda: StrategyGateStore(tmp_path / "gates.json")
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_a_gate_saved_on_a_strategy_is_listed_replaced_and_deleted(client: httpx.AsyncClient) -> None:
    async with client:
        created = await client.post(
            _EMA, json={"label": "EMA gap clears the minimum", "expression": "EMA5 − EMA10 − gap", "sign": "gt"}
        )
        assert created.status_code == 201, created.text
        gate = created.json()
        assert [(t["coefficient"], t["variable"]) for t in gate["terms"]] == [
            (1.0, "EMA5"),
            (-1.0, "EMA10"),
            (-1.0, "gap"),
        ]

        listed = (await client.get(_EMA)).json()
        assert [g["gate_id"] for g in listed["gates"]] == [gate["gate_id"]]
        # Saved on the strategy, not on any other one.
        assert (await client.get("/api/strategy-gates/sma_crossover")).json()["gates"] == []

        replaced = await client.put(
            f"{_EMA}/{gate['gate_id']}",
            json={"label": "Close above EMA 10", "expression": "close - EMA10", "sign": "gt"},
        )
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["created_at_ms"] == gate["created_at_ms"]

        deleted = await client.delete(f"{_EMA}/{gate['gate_id']}")
        assert deleted.status_code == 204
        assert (await client.get(_EMA)).json()["gates"] == []


@pytest.mark.parametrize(
    ("expression", "reason"),
    [("EMA5 * EMA10", "cannot multiply"), ("FOO - 1", "not a value"), ("close > 0", "Leave the comparison out")],
)
async def test_an_invalid_gate_is_never_saved(client: httpx.AsyncClient, expression: str, reason: str) -> None:
    async with client:
        refused = await client.post(_EMA, json={"label": "bad", "expression": expression, "sign": "gt"})
        assert (await client.get(_EMA)).json()["gates"] == []

    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert detail["code"] == "GATE_EXPRESSION_REFUSED"
    assert reason in detail["message"]


async def test_a_number_too_large_to_judge_is_refused_and_leaves_the_store_readable(client: httpx.AsyncClient) -> None:
    async with client:
        refused = await client.post(
            _EMA, json={"label": "huge", "expression": "1" + "0" * 330 + " * EMA5", "sign": "gt"}
        )
        listed = await client.get("/api/strategy-gates/sma_crossover")

    assert (refused.status_code, refused.json()["detail"]["code"]) == (422, "GATE_EXPRESSION_REFUSED")
    assert (listed.status_code, listed.json()["gates"]) == (200, [])


@pytest.mark.parametrize("expression", ["AROON25 - 50", "STOCHRSI14 - 50", "FISHER9"])
async def test_a_multi_line_indicator_is_refused_when_saved_and_when_judged(
    client: httpx.AsyncClient, expression: str
) -> None:
    async with client:
        saved = await client.post(_EMA, json={"label": "multi", "expression": expression, "sign": "gt"})
        judged = await client.post(
            f"{_EMA}/evaluate",
            json={
                "symbol": "SPY",
                "candles": [_candle(1_790_700_000_000, 100.0, 50.0)],
                "draft": {"label": "multi", "expression": expression, "sign": "gt"},
            },
        )

    assert (saved.status_code, saved.json()["detail"]["code"]) == (422, "GATE_EXPRESSION_REFUSED")
    assert (judged.status_code, judged.json()["detail"]["code"]) == (422, "GATE_EXPRESSION_REFUSED")


async def test_an_unreadable_gate_store_is_a_503_naming_the_store(client: httpx.AsyncClient, tmp_path: Path) -> None:
    (tmp_path / "gates.json").write_text("{not json", encoding="utf-8")
    async with client:
        listed = await client.get(_EMA)

    assert (listed.status_code, listed.json()["detail"]["code"]) == (503, "GATE_STORE_UNAVAILABLE")


async def test_the_catalogue_lists_what_a_gate_can_read_as_it_is_written(client: httpx.AsyncClient) -> None:
    async with client:
        listed = await client.get("/api/strategy-gates/catalogue")

    assert listed.status_code == 200, listed.text
    by_name = {entry["name"]: entry for entry in listed.json()["indicators"]}
    assert by_name["ema"]["variable"] == "EMA10"
    assert (by_name["ema"]["min_length"], by_name["ema"]["max_length"]) == (1, 500)
    assert by_name["vwap"]["variable"] == "VWAP"
    assert "aroon" not in by_name


async def test_an_unknown_strategy_or_gate_is_a_plain_404(client: httpx.AsyncClient) -> None:
    async with client:
        strategy = await client.get("/api/strategy-gates/retired_strategy")
        gate = await client.delete(f"{_EMA}/g-000000000000")

    assert (strategy.status_code, strategy.json()["detail"]["code"]) == (404, "STRATEGY_VIEW_UNAVAILABLE")
    assert (gate.status_code, gate.json()["detail"]["code"]) == (404, "GATE_NOT_FOUND")


def _candle(close_ms: int, close: float, rsi: float | None) -> dict:
    return {
        "bar_close_ms": close_ms,
        "open": close - 0.5,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": 1000,
        "values": {"ema_fast": close, "ema_slow": close - 0.1, "rsi": rsi},
    }


async def test_evaluate_judges_saved_gates_and_a_draft_on_the_callers_candles(client: httpx.AsyncClient) -> None:
    candles = [_candle(1_790_700_000_000 + i * 900_000, 100.0 + i, rsi) for i, rsi in enumerate([None, 45.0, 55.0])]
    async with client:
        gate = (await client.post(_EMA, json={"label": "RSI over 50", "expression": "RSI14 - 50", "sign": "gt"})).json()
        judged = await client.post(
            f"{_EMA}/evaluate",
            json={
                "symbol": "SPY",
                "settings": {"gap": 0.2, "rsi_min": 50.0, "rsi_max": 70.0},
                "candles": candles,
                "draft": {"label": "draft", "expression": "close - open - gap", "sign": "gt"},
            },
        )

    assert judged.status_code == 200, judged.text
    body = judged.json()
    assert body["results"] == {gate["gate_id"]: [None, False, True], "draft": [True, True, True]}
    assert body["chart_computed"] == []
