"""HTTP seam tests for the aggregated bot gallery router (Task 4).

Drives ``/gallery/snapshot`` and ``/gallery/stream`` through the real FastAPI
app via ``ASGITransport``, with ``get_gallery_hub`` overridden via
``app.dependency_overrides`` to a ``GalleryHub`` built from fakes (fakes'
shape mirrors ``tests/services/test_gallery_hub.py``).
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from httpx import ASGITransport

from app.config import settings
from app.main import app
from app.routers import broker_v2_gallery
from app.schemas.broker_v2_panel import PanelAction
from app.services.broker_v2_panel import panel_chart_data_source, panel_data_source
from app.services.broker_v2_panel.gallery_hub import GalleryHub
from app.services.broker_v2_panel.panel_data_source import PanelUnavailableError, UnknownBotError

_BROKER = "alpaca"
_ACCOUNT_ID = "PA3"
_URL_PREFIX = f"/api/brokers/{_BROKER}/accounts/{_ACCOUNT_ID}/gallery"


def _status_label_for(*, phase: str, running: bool) -> str:
    """Mirrors ``catalog_projection_service.status_label_for`` so this fake
    carries an accurate ``status_label`` — the field ``GalleryHub`` actually
    reads (``_is_retired``), not the raw ``phase``."""
    if phase == "RETIRED":
        return "Retired"
    return "Working" if running else "Off duty"


class _Cat2:
    """Catalog stub mirroring the ``BotCatalogView`` fields ``GalleryHub`` reads."""

    def __init__(
        self,
        sid: str,
        symbol: str,
        running: bool,
        realized_pnl_today: float,
        open_pnl: float,
        fills_today: int,
        *,
        strategy_label: str = "",
        needs_attention: bool = False,
        phase: str = "ON_DUTY",
    ) -> None:
        self.strategy_instance_id = sid
        self.symbol = symbol
        self.running = running
        self.strategy_label = strategy_label
        self.realized_pnl_today = realized_pnl_today
        self.open_pnl = open_pnl
        self.fills_today = fills_today
        self.needs_attention = needs_attention
        self.phase = phase

    @property
    def status_label(self) -> str:
        # A property, not an init-time value: several tests mutate
        # ``.phase``/``.running`` in place mid-test to simulate a poll
        # observing a changed row, mirroring how a freshly-fetched
        # production catalog row's status_label is always derived fresh.
        return _status_label_for(phase=self.phase, running=self.running)


class _FakeCatalogSource:
    def __init__(self, rows: list[_Cat2]) -> None:
        self._rows = rows

    async def get_catalog(self, broker: str, account_id: str) -> list[_Cat2]:
        return self._rows


class _FakeAggregator:
    """Mirrors the REAL contract: ``ensure_subscribed`` is async, ``snapshot`` is sync."""

    def __init__(self) -> None:
        self.subscribed: list[str] = []

    async def ensure_subscribed(self, symbol: str) -> None:
        self.subscribed.append(symbol)

    def snapshot(self, symbol: str, since_ms: int | None = None) -> list[object]:
        return [
            type(
                "B",
                (),
                {
                    "start_ms": 1_700_000_000_000,
                    "end_ms": 1_700_000_060_000,
                    "open": 1.0,
                    "high": 1.2,
                    "low": 0.9,
                    "close": 1.1,
                    "volume": 100,
                    "source": "ibkr",
                },
            )()
        ]


def _fake_hub() -> GalleryHub:
    rows = [_Cat2("Aug11-02", "SPY", True, 142.0, -8.0, 12)]
    return GalleryHub(
        broker=_BROKER,
        account_id=_ACCOUNT_ID,
        catalog_source=_FakeCatalogSource(rows),
        aggregator=_FakeAggregator(),
    )


@pytest.fixture
def gallery_app(monkeypatch: pytest.MonkeyPatch):
    # These routes carry PROTECTED_DATA_PLANE_READ_DEPENDENCIES in main.py
    # (same sensitivity as broker_v2_panel); opt out of the shared-secret
    # check the way tests/routers/test_preview_action_plan.py does for other
    # protected-read routes, rather than modeling the local secret hop here.
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", True)
    app.dependency_overrides[broker_v2_gallery.get_gallery_hub] = _fake_hub
    try:
        yield app
    finally:
        app.dependency_overrides.pop(broker_v2_gallery.get_gallery_hub, None)


async def test_gallery_snapshot_returns_running_bots(gallery_app) -> None:
    transport = ASGITransport(app=gallery_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"{_URL_PREFIX}/snapshot")

    assert response.status_code == 200
    body = response.json()
    assert body["resolution"] == "1m"
    assert "bots" in body and "symbols" in body
    assert {b["sid"] for b in body["bots"]} == {"Aug11-02"}
    assert {s["symbol"] for s in body["symbols"]} == {"SPY"}
    assert body["bots"][0]["last_bar_at_ms"] == 1_700_000_060_000  # SPY's latest bar end_ms


async def test_gallery_snapshot_includes_stopped_bot_and_excludes_retired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The snapshot shows every non-retired bot — a stopped bot projects
    with ``running=False`` + a Resume action and its symbol's bars are
    fetched, while a retired bot (and its otherwise-unwatched symbol) never
    reaches the payload."""
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", True)
    rows = [
        _Cat2("Aug11-02", "SPY", True, 142.0, -8.0, 12, phase="ON_DUTY"),
        _Cat2("Aug11-03", "QQQ", False, 5.0, 0.0, 1, phase="OFF_DUTY"),
        _Cat2("Aug11-04", "IWM", False, None, None, None, phase="RETIRED"),
    ]
    hub = GalleryHub(
        broker=_BROKER,
        account_id=_ACCOUNT_ID,
        catalog_source=_FakeCatalogSource(rows),
        aggregator=_FakeAggregator(),
    )
    app.dependency_overrides[broker_v2_gallery.get_gallery_hub] = lambda: hub
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(f"{_URL_PREFIX}/snapshot")
    finally:
        app.dependency_overrides.pop(broker_v2_gallery.get_gallery_hub, None)

    assert response.status_code == 200
    body = response.json()
    assert {b["sid"] for b in body["bots"]} == {"Aug11-02", "Aug11-03"}  # retired excluded
    assert {s["symbol"] for s in body["symbols"]} == {"SPY", "QQQ"}  # IWM (retired) never subscribed
    stopped = next(b for b in body["bots"] if b["sid"] == "Aug11-03")
    assert stopped["running"] is False
    assert stopped["primary_action"]["action_id"] == "resume"
    assert stopped["primary_action"]["label"] == "Resume"
    assert stopped["primary_action"]["enabled"] is False


async def test_gallery_stream_stopped_bot_survives_update() -> None:
    """A bot that stops between polls stays on the wall as a stopped tile —
    it must not appear in ``removed_sids``."""
    rows = [_Cat2("Aug11-02", "SPY", True, 142.0, -8.0, 12)]
    hub = GalleryHub(
        broker=_BROKER,
        account_id=_ACCOUNT_ID,
        catalog_source=_FakeCatalogSource(rows),
        aggregator=_FakeAggregator(),
    )

    response = await broker_v2_gallery.stream_gallery(cursor=None, hub=hub)
    try:
        await asyncio.wait_for(response.body_iterator.__anext__(), timeout=5.0)  # snapshot frame

        rows[0].running = False  # bot stops before the next poll

        frame = await asyncio.wait_for(response.body_iterator.__anext__(), timeout=5.0)
    finally:
        await response.body_iterator.aclose()

    assert "event: update" in frame
    payload = _frame_payload(frame)
    assert payload["removed_sids"] == []
    assert {b["sid"] for b in payload["bots_delta"]} == {"Aug11-02"}
    assert payload["bots_delta"][0]["running"] is False
    assert payload["bots_delta"][0]["primary_action"]["action_id"] == "resume"
    assert payload["bots_delta"][0]["primary_action"]["enabled"] is False


def _frame_payload(frame: str) -> dict:
    data_line = next(line for line in frame.splitlines() if line.startswith("data: "))
    return json.loads(data_line[len("data: ") :])


async def _read_first_frame(hub: GalleryHub, *, cursor: str | None) -> str:
    """Drive ``stream_gallery`` directly and return its first SSE frame.

    ``httpx.ASGITransport`` collects the ENTIRE ASGI response body before
    returning anything to the client (see
    ``httpx._transports.asgi.ASGITransport.handle_async_request``: it
    ``await``s ``self.app(scope, receive, send)`` to full completion before
    constructing a ``Response``) — verified empirically: driving this
    never-terminating poll loop through ``client.stream(...)`` hangs until a
    wrapping ``asyncio.wait_for`` guard times out, because the generator
    never reaches ``more_body=False``. Every existing SSE test in this repo
    that uses ``client.stream`` (``test_broker_activity.py``,
    ``test_panel_router.py``) works around this the same way: the fake
    producer is made to terminate (a sentinel item, or an explicit
    ``publisher.stop()``) so the ASGI call actually completes.
    ``GalleryHub``'s poll loop has no such external stop signal, so this
    instead calls the router's actual ``stream_gallery`` coroutine (the
    exact function FastAPI would call) and reads its ``StreamingResponse``'s
    ``body_iterator`` (the real production async generator) directly, which
    is the only way to observe one frame without waiting for the (here,
    infinite) stream to end. Callers wrap this in ``asyncio.wait_for`` so a
    regression here fails fast instead of hanging the suite.
    """
    response = await broker_v2_gallery.stream_gallery(cursor=cursor, hub=hub)
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    try:
        return await response.body_iterator.__anext__()
    finally:
        await response.body_iterator.aclose()


async def test_gallery_stream_first_frame_is_snapshot() -> None:
    hub = _fake_hub()

    frame = await asyncio.wait_for(_read_first_frame(hub, cursor=None), timeout=5.0)

    assert "event: snapshot" in frame
    payload = _frame_payload(frame)
    assert payload["resolution"] == "1m"
    assert payload["stream_epoch"] == hub._epoch
    assert payload["surface_version"] == 1


def test_gallery_hub_epoch_has_per_process_nonce() -> None:
    """The epoch is ``broker:account_id:<nonce>`` — a bare ``broker:account_id``
    would be byte-identical across a data-plane restart, so a reconnecting
    client's stale cursor would never mismatch and ``event: reset`` would
    never fire (see ``gallery_hub.py``'s ``_PROCESS_NONCE`` docstring)."""
    hub = _fake_hub()

    prefix = f"{_BROKER}:{_ACCOUNT_ID}:"
    assert hub._epoch.startswith(prefix)
    assert len(hub._epoch) > len(prefix)


async def test_gallery_stream_stale_cursor_emits_reset_first() -> None:
    """A cursor from a different epoch gets ``event: reset`` before the snapshot."""
    hub = _fake_hub()
    stale_cursor = "stale-epoch:0"

    frame = await asyncio.wait_for(_read_first_frame(hub, cursor=stale_cursor), timeout=5.0)

    assert "event: reset" in frame
    payload = _frame_payload(frame)
    assert payload["reason"] == "epoch_changed"
    assert payload["cursor"] == f"{hub._epoch}:1"


async def test_gallery_stream_matching_cursor_skips_reset() -> None:
    """A cursor already on the hub's epoch goes straight to ``event: snapshot``."""
    hub = _fake_hub()
    matching_cursor = f"{hub._epoch}:0"

    frame = await asyncio.wait_for(_read_first_frame(hub, cursor=matching_cursor), timeout=5.0)

    assert "event: reset" not in frame
    assert "event: snapshot" in frame


@pytest.mark.parametrize("error", [PanelUnavailableError("clerk unavailable"), UnknownBotError("no such bot")])
async def test_panel_chart_fill_source_degrades_to_empty_on_panel_error(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """``_PanelChartFillSource`` exists specifically so one bot's unavailable
    fill evidence never fails the whole gallery snapshot for every other bot
    (its own docstring). Exercise the REAL adapter — not a fake that already
    assumes the contract — against both exception types
    ``panel_chart_data_source.resolve_symbol_and_fills`` can raise, and
    assert it degrades to an empty result instead of propagating."""

    async def _raise(*args: object, **kwargs: object) -> tuple[str, tuple]:
        raise error

    monkeypatch.setattr(panel_chart_data_source, "resolve_symbol_and_fills", _raise)
    source = broker_v2_gallery._PanelChartFillSource()

    symbol, fills = await source.resolve_symbol_and_fills(
        _BROKER, _ACCOUNT_ID, "some-sid", now_ms=1_700_000_000_000
    )

    assert symbol == ""
    assert fills == ()


@pytest.mark.parametrize(
    "error",
    [PanelUnavailableError("clerk unavailable"), UnknownBotError("no such bot")],
)
async def test_panel_primary_action_source_degrades_to_none_on_panel_error(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    async def _raise(*args: object, **kwargs: object) -> object:
        raise error

    monkeypatch.setattr(panel_data_source, "get_panel", _raise)

    action = await broker_v2_gallery._PanelPrimaryActionSource().resolve_resume_action(
        _BROKER, _ACCOUNT_ID, "some-sid"
    )

    assert action is None


async def test_panel_primary_action_source_returns_authoritative_resume_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resume = PanelAction(
        action_id="resume",
        label="Resume",
        explanation="Resume is blocked by current admission evidence.",
        enabled=False,
        blockers=[],
        confirmation=None,
        revision=7,
        concurrency_token="resume-token",
    )
    panel = type("Panel", (), {"actions": [resume]})()

    async def _get_panel(*args: object, **kwargs: object) -> object:
        return panel

    monkeypatch.setattr(panel_data_source, "get_panel", _get_panel)

    action = await broker_v2_gallery._PanelPrimaryActionSource().resolve_resume_action(
        _BROKER, _ACCOUNT_ID, "some-sid"
    )

    assert action is resume
