"""Router tests for run-scoped bot-event stream backfill."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers.bot_events import bot_event_stream
from app.schemas.bot_events import (
    BotEventIdentity,
    BotEventRaw,
    BotEventRawType,
    GateStep,
    GateStepResult,
    SourceAuthority,
)
from app.services.bot_event_stream_service import BotEventStreamService
from app.services.bot_event_wal import BotEventRawWal

pytestmark = pytest.mark.asyncio

RUN_ID = "run-bot-events-" + "a" * 48
SID = "bot-events-sid"


@pytest.fixture
def live_runs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "live_runs"
    root.mkdir()

    from app.broker.ibkr import config as ibkr_config

    ibkr_config.reset_settings_for_testing()
    monkeypatch.setenv("IBKR_LIVE_RUNS_ROOT", str(root))
    ibkr_config.reset_settings_for_testing()

    yield root

    ibkr_config.reset_settings_for_testing()


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _run_dir(root: Path, run_id: str = RUN_ID) -> Path:
    path = root / run_id
    path.mkdir(parents=True)
    return path


def _raw(
    *,
    seq: int,
    event_type: BotEventRawType,
    identity: BotEventIdentity,
    ts_ms: int | None = None,
    gate_step: GateStep | None = None,
) -> BotEventRaw:
    return BotEventRaw(
        seq=seq,
        ts_ms=ts_ms or 1_700_000_000_000 + seq,
        strategy_instance_id=SID,
        run_id=RUN_ID,
        event_type=event_type,
        source_authority=SourceAuthority.ENGINE_LOOP,
        identity=identity,
        gate_step=gate_step,
    )


def _gate_step(seq: int, *, result: GateStepResult = GateStepResult.PASS) -> BotEventRaw:
    step = GateStep(
        evaluation_id="eval-1",
        gate_id="readiness.session",
        gate_result=result,
        source_authority=SourceAuthority.ENGINE_LOOP,
    )
    return _raw(
        seq=seq,
        event_type=BotEventRawType.GATE_STEP,
        identity=BotEventIdentity(evaluation_id="eval-1"),
        gate_step=step,
    )


def _append(run_dir: Path, events: list[BotEventRaw]) -> None:
    wal = BotEventRawWal(run_dir / "bot_events.jsonl")
    for event in events:
        wal.append_event(event)


async def test_bot_events_backfill_pages_authored_rows_without_splitting_gate_walk(
    live_runs_root: Path,
) -> None:
    run_dir = _run_dir(live_runs_root)
    _append(
        run_dir,
        [
            _gate_step(1),
            _raw(
                seq=2,
                event_type=BotEventRawType.ORDER_SUBMITTED,
                identity=BotEventIdentity(
                    evaluation_id="eval-1",
                    intent_id="intent-1",
                    order_ref="learn-ai/bot-events/v1:intent-1",
                ),
            ),
            _raw(
                seq=3,
                event_type=BotEventRawType.EVALUATION_IDLE,
                identity=BotEventIdentity(evaluation_id="eval-2"),
            ),
        ],
    )

    async with _client() as client:
        response = await client.get(
            f"/api/live-runs/{RUN_ID}/bot-events",
            params={"limit": 1},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["next_seq"] == 2
        assert body["next_cursor"].endswith(":2")
        assert body["durable_stream_id"] in body["next_cursor"]
        assert [row["seq"] for row in body["rows"]] == [2]
        assert body["rows"][0]["event_type"] == "order_submitted"
        assert [step["gate_id"] for step in body["rows"][0]["gate_steps"]] == [
            "readiness.session"
        ]

        response = await client.get(
            f"/api/live-runs/{RUN_ID}/bot-events",
            params={"cursor": body["next_cursor"], "limit": 1},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["next_seq"] is None
        assert body["next_cursor"] is None
        assert body["high_water_cursor"].endswith(":3")
        assert [row["seq"] for row in body["rows"]] == [3]
        assert body["rows"][0]["event_type"] == "evaluation_idle"


async def test_bot_events_backfill_ignores_invisible_pass_only_gate_steps(
    live_runs_root: Path,
) -> None:
    run_dir = _run_dir(live_runs_root)
    _append(run_dir, [_gate_step(1)])

    async with _client() as client:
        response = await client.get(f"/api/live-runs/{RUN_ID}/bot-events")

    assert response.status_code == 200
    payload = response.json()
    assert payload["rows"] == []
    assert payload["next_seq"] is None
    assert payload["high_water_cursor"].endswith(":0")
    assert payload["durable_stream_id"] in payload["high_water_cursor"]


async def test_bot_events_backfill_cursor_uses_seq_not_timestamp_order(
    live_runs_root: Path,
) -> None:
    run_dir = _run_dir(live_runs_root)
    _append(
        run_dir,
        [
            _raw(
                seq=1,
                ts_ms=1_700_000_000_200,
                event_type=BotEventRawType.SIGNAL_FIRED,
                identity=BotEventIdentity(evaluation_id="eval-1"),
            ),
            _raw(
                seq=2,
                ts_ms=1_700_000_000_100,
                event_type=BotEventRawType.SIGNAL_FIRED,
                identity=BotEventIdentity(evaluation_id="eval-2"),
            ),
        ],
    )

    async with _client() as client:
        response = await client.get(
            f"/api/live-runs/{RUN_ID}/bot-events",
            params={"limit": 1},
        )
        assert response.status_code == 200
        body = response.json()
        assert [row["seq"] for row in body["rows"]] == [1]
        assert body["next_seq"] == 1

        response = await client.get(
            f"/api/live-runs/{RUN_ID}/bot-events",
            params={"after_seq": body["next_seq"], "limit": 1},
        )
        assert response.status_code == 200
        body = response.json()
        assert [row["seq"] for row in body["rows"]] == [2]
        assert body["next_seq"] is None


async def test_bot_events_backfill_404s_unknown_run(live_runs_root: Path) -> None:
    async with _client() as client:
        response = await client.get(f"/api/live-runs/{RUN_ID}/bot-events")

    assert response.status_code == 404


async def test_bot_events_backfill_rejects_invalid_run_id(
    live_runs_root: Path,
) -> None:
    async with _client() as client:
        response = await client.get("/api/live-runs/bad%20run/bot-events")

    assert response.status_code == 400


async def test_bot_events_backfill_reports_unprojectable_history(
    live_runs_root: Path,
) -> None:
    run_dir = _run_dir(live_runs_root)
    (run_dir / "bot_events.jsonl").write_text("{not-json}\n", encoding="utf-8")

    async with _client() as client:
        response = await client.get(f"/api/live-runs/{RUN_ID}/bot-events")

    assert response.status_code == 503
    assert response.json()["detail"] == "bot-event stream history cannot be projected"


async def test_bot_events_backfill_rejects_replaced_stream_cursor(
    live_runs_root: Path,
) -> None:
    run_dir = _run_dir(live_runs_root)
    _append(
        run_dir,
        [
            _raw(
                seq=1,
                event_type=BotEventRawType.SIGNAL_FIRED,
                identity=BotEventIdentity(evaluation_id="eval-1"),
            )
        ],
    )

    async with _client() as client:
        response = await client.get(
            f"/api/live-runs/{RUN_ID}/bot-events",
            params={"cursor": "replaced-stream:0"},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "EVENT_STREAM_REPLACED"


async def test_bot_events_stream_404s_unknown_run(live_runs_root: Path) -> None:
    async with _client() as client:
        response = await client.get(f"/api/live-runs/{RUN_ID}/bot-events/stream")

    assert response.status_code == 404


async def test_bot_events_stream_rejects_invalid_run_id(
    live_runs_root: Path,
) -> None:
    async with _client() as client:
        response = await client.get("/api/live-runs/bad%20run/bot-events/stream")

    assert response.status_code == 400


async def test_bot_events_stream_response_uses_sse_headers(
    live_runs_root: Path,
) -> None:
    _run_dir(live_runs_root)

    response = await bot_event_stream(RUN_ID, service=BotEventStreamService())

    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"


async def test_bot_events_stream_reports_projection_error_without_exception_detail(
    live_runs_root: Path,
) -> None:
    run_dir = _run_dir(live_runs_root)
    (run_dir / "bot_events.jsonl").write_text("{not-json}\n", encoding="utf-8")

    service = BotEventStreamService()
    response = await bot_event_stream(RUN_ID, service=service)
    chunk = await anext(response.body_iterator)
    text = chunk.decode() if isinstance(chunk, bytes) else chunk

    assert "event: error" in text
    assert "bot-event stream history cannot be projected" in text
    assert "not-json" not in text
    assert service._channels == {}


async def test_bot_events_stream_resumes_from_last_event_id(
    live_runs_root: Path,
) -> None:
    run_dir = _run_dir(live_runs_root)
    _append(
        run_dir,
        [
            _raw(
                seq=1,
                event_type=BotEventRawType.SIGNAL_FIRED,
                identity=BotEventIdentity(evaluation_id="eval-1"),
            ),
            _raw(
                seq=2,
                event_type=BotEventRawType.SIGNAL_FIRED,
                identity=BotEventIdentity(evaluation_id="eval-2"),
            ),
        ],
    )
    service = BotEventStreamService()
    channel = service.channel_for_run(run_dir)

    response = await bot_event_stream(
        RUN_ID,
        cursor=f"{channel.stream_id}:0",
        last_event_id=f"{channel.stream_id}:1",
        service=service,
    )
    try:
        chunk = await anext(response.body_iterator)
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
    finally:
        await response.body_iterator.aclose()
        await service.stop_all()

    assert f"id: {channel.stream_id}:2" in text
    assert '"seq":2' in text
    assert '"seq":1' not in text
    assert service._channels == {}


async def test_bot_events_legacy_stream_deep_backfills_beyond_queue_capacity(
    live_runs_root: Path,
) -> None:
    run_dir = _run_dir(live_runs_root)
    _append(
        run_dir,
        [
            _raw(
                seq=seq,
                event_type=BotEventRawType.SIGNAL_FIRED,
                identity=BotEventIdentity(evaluation_id=f"eval-{seq}"),
            )
            for seq in range(1, 71)
        ],
    )
    service = BotEventStreamService()
    response = await bot_event_stream(RUN_ID, since_seq=0, service=service)
    chunks: list[str] = []
    try:
        for _ in range(70):
            chunk = await anext(response.body_iterator)
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    finally:
        await response.body_iterator.aclose()

    text = "".join(chunks)
    assert "event: gap" not in text
    assert [
        int(part.split('"seq":', 1)[1].split(",", 1)[0])
        for part in chunks
    ] == list(range(1, 71))
    assert service._channels == {}


async def test_bot_events_legacy_reconnect_deep_backfills_from_last_event_id(
    live_runs_root: Path,
) -> None:
    run_dir = _run_dir(live_runs_root)
    _append(
        run_dir,
        [
            _raw(
                seq=seq,
                event_type=BotEventRawType.SIGNAL_FIRED,
                identity=BotEventIdentity(evaluation_id=f"eval-{seq}"),
            )
            for seq in range(1, 71)
        ],
    )
    service = BotEventStreamService()
    channel = service.channel_for_run(run_dir)
    response = await bot_event_stream(
        RUN_ID,
        since_seq=0,
        last_event_id=f"{channel.stream_id}:65",
        service=service,
    )
    chunks: list[str] = []
    try:
        for _ in range(5):
            chunk = await anext(response.body_iterator)
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    finally:
        await response.body_iterator.aclose()

    assert "event: gap" not in "".join(chunks)
    assert [
        int(part.split('"seq":', 1)[1].split(",", 1)[0])
        for part in chunks
    ] == list(range(66, 71))
    assert service._channels == {}
