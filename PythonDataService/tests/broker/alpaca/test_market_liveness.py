"""Tests for the owned Alpaca real-time market-liveness source (#1671)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import FastAPI

from app.broker.alpaca.market_liveness import AlpacaMarketLivenessConsumer, read_shared_market_status
from app.broker.capture.journal import CaptureJournal
from app.broker.contract.models import BrokerClockEvidence
from app.schemas.market_liveness import MarketLivenessFact, MarketStatusSnapshot, SymbolTradingStatusEvidence
from app.services.market_liveness import MARKET_CLOCK_MAX_AGE_MS, MarketLivenessStore, get_market_liveness_store

_NOW = 1_700_000_000_000


def _iso_ms(ms: int) -> str:
    """Test-only RFC-3339 rendering of an exact ms offset (never a wire/storage format)."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


class _Read:
    async def get_clock_evidence(self) -> BrokerClockEvidence:
        return BrokerClockEvidence(
            broker="alpaca",
            is_open=True,
            vendor_timestamp_ms=_NOW,
            next_open_ms=None,
            next_close_ms=None,
            observed_at_ms=_NOW,
        )


def _consumer(tmp_path: Path, store: MarketLivenessStore) -> AlpacaMarketLivenessConsumer:
    async def _frames():
        if False:  # pragma: no cover - establishes an injected empty iterator.
            yield ""

    return AlpacaMarketLivenessConsumer(
        read=_Read(),  # type: ignore[arg-type]
        frame_source=_frames,
        store=store,
        journal=CaptureJournal(capture_dir=tmp_path / "capture", clock=lambda: _NOW),
        clock=lambda: _NOW,
        max_reconnects=0,
    )


@pytest.mark.asyncio
async def test_clock_poll_and_status_transition_compose_the_shared_fact(tmp_path: Path) -> None:
    store = MarketLivenessStore()
    consumer = _consumer(tmp_path, store)

    await consumer.refresh_clock()
    consumer.handle_frame(json.dumps([{"T": "s", "S": "SPY", "sc": "H", "t": "2023-11-14T22:13:20Z"}]))

    halted = store.fact("SPY", now_ms=_NOW)

    assert halted.state == "HALTED"
    assert halted.reason_code == "SYMBOL_HALTED"
    assert halted.symbol_status is not None
    assert halted.symbol_status.reason_code == "ALPACA_STATUS_HALT"

    consumer.handle_frame(json.dumps([{"T": "s", "S": "SPY", "sc": "T", "t": "2023-11-14T22:13:21Z"}]))

    resumed = store.fact("SPY", now_ms=_NOW)

    assert resumed.state == "TRADABLE"
    assert resumed.reason_code == "MARKET_TRADABLE"
    assert resumed.symbol_status is not None
    assert resumed.symbol_status.reason_code == "ALPACA_STATUS_RESUME"


def test_bad_status_frame_does_not_clear_prior_symbol_evidence(tmp_path: Path) -> None:
    """A single malformed frame refuses evidence for itself only — it must
    not wipe previously-established evidence for other symbols. Only a
    genuine reconnect invalidates the halted-symbol map (#1671): the status
    stream sends transitions, not a heartbeat, so treating every bad frame
    as connection-wide corruption would let one unrelated garbled message
    silently un-halt a symbol that was legitimately proven halted earlier in
    the same connection cycle."""
    store = MarketLivenessStore()
    store.observe_clock(
        BrokerClockEvidence(
            broker="alpaca",
            is_open=True,
            vendor_timestamp_ms=_NOW,
            next_open_ms=None,
            next_close_ms=None,
            observed_at_ms=_NOW,
        )
    )
    store.observe_symbol_status(
        SymbolTradingStatusEvidence(
            symbol="SPY",
            state="HALTED",
            source="test.status",
            observed_at_ms=_NOW,
            source_timestamp_ms=_NOW,
        )
    )
    consumer = _consumer(tmp_path, store)

    consumer.handle_frame("not-json")

    fact = store.fact("SPY", now_ms=_NOW)
    assert fact.state == "HALTED"
    assert fact.reason_code == "SYMBOL_HALTED"


@pytest.mark.parametrize("frame", [
    "not-json",
    '[{"T":"success","msg":"connected"}]',
    '[{"T":"success","msg":"authenticated"}]',
    '[{"T":"error","code":406,"msg":"connection limit exceeded"}]',
    '[{"T":"error","code":401,"msg":"not authenticated"}]',
    '[{"T":"subscription","statuses":[]}]',
    '[{"T":"subscription","statuses":"*"}]',
    '[{"T":"s","S":"SPY","sc":"T"}]',
])
def test_unsubscribed_frames_never_prove_market_liveness(tmp_path: Path, frame: str) -> None:
    """A rejected handshake must never briefly admit a Paper or Shadow run."""
    store = MarketLivenessStore()
    store.observe_clock(
        BrokerClockEvidence(
            broker="alpaca",
            is_open=True,
            vendor_timestamp_ms=_NOW,
            next_open_ms=None,
            next_close_ms=None,
            observed_at_ms=_NOW,
        )
    )
    consumer = _consumer(tmp_path, store)

    consumer.handle_frame(frame)

    fact = store.fact("SPY", now_ms=_NOW)
    assert fact.state == "UNKNOWN"
    assert fact.reason_code == "STATUS_STREAM_DISCONNECTED"


async def test_status_subscription_proves_quiet_symbols_until_vendor_refusal(tmp_path: Path) -> None:
    store = MarketLivenessStore()
    consumer = _consumer(tmp_path, store)
    await consumer.refresh_clock()

    consumer.handle_frame('[{"T":"subscription","statuses":["*"]}]')
    assert store.fact("SPY", now_ms=_NOW).state == "TRADABLE"

    consumer.handle_frame('[{"T":"error","code":406,"msg":"connection limit exceeded"}]')
    assert store.fact("SPY", now_ms=_NOW).state == "UNKNOWN"


async def test_paper_worker_shares_statuses_without_opening_another_vendor_socket(tmp_path: Path) -> None:
    source = MarketLivenessStore()
    primary = _consumer(tmp_path, source)
    primary.handle_frame('[{"T":"subscription","statuses":["*"]}]')
    paper = MarketLivenessStore()

    async def snapshot_source() -> MarketStatusSnapshot:
        return source.status_snapshot(now_ms=_NOW)

    async def forbidden_vendor_socket():
        raise AssertionError("A Paper follower must not open another stock-data connection")
        yield  # pragma: no cover

    consumer = AlpacaMarketLivenessConsumer(
        read=_Read(),  # type: ignore[arg-type]
        frame_source=forbidden_vendor_socket,
        status_snapshot_source=snapshot_source,
        store=paper,
        clock=lambda: _NOW,
    )
    consumer.start()
    try:
        for _ in range(20):
            await asyncio.sleep(0)
            if paper.fact("SPY", now_ms=_NOW).state == "TRADABLE":
                break
        assert paper.fact("SPY", now_ms=_NOW).state == "TRADABLE"

        primary.handle_frame(json.dumps([{"T":"s","S":"SPY","sc":"H","t":_iso_ms(_NOW)}]))
        await consumer.refresh_shared_status()
        halted = paper.fact("SPY", now_ms=_NOW)
        assert halted.state == "HALTED"
        assert halted.symbol_status == source.status_snapshot(now_ms=_NOW).symbol_statuses[0]

        source.clear_symbol_statuses()  # A source restart cannot erase the follower's known halt.
        await consumer.refresh_shared_status()
        assert paper.fact("SPY", now_ms=_NOW).state == "HALTED"

        primary.handle_frame(json.dumps([{"T":"s","S":"SPY","sc":"T","t":_iso_ms(_NOW + 1)}]))
        await consumer.refresh_shared_status()
        assert paper.fact("SPY", now_ms=_NOW).state == "TRADABLE"

        source.mark_stream_disconnected(observed_at_ms=_NOW)
        await consumer.refresh_shared_status()
        assert paper.fact("SPY", now_ms=_NOW).state == "UNKNOWN"
    finally:
        await consumer.stop()


async def test_shared_status_proof_expires_even_if_the_poll_task_stalls(tmp_path: Path) -> None:
    source = MarketLivenessStore()
    source.mark_stream_connected(observed_at_ms=_NOW)
    paper = MarketLivenessStore()
    await _consumer(tmp_path, paper).refresh_clock()
    paper.apply_status_snapshot(source.status_snapshot(now_ms=_NOW), now_ms=_NOW)
    later = _NOW + MARKET_CLOCK_MAX_AGE_MS + 1
    paper.observe_clock((await _Read().get_clock_evidence()).model_copy(update={"observed_at_ms":later}))

    assert paper.fact("SPY", now_ms=later).state == "UNKNOWN"
    exported = paper.status_snapshot(now_ms=later)
    assert exported.connected is False
    assert exported.observed_at_ms == _NOW


@pytest.mark.parametrize("age", [-1, MARKET_CLOCK_MAX_AGE_MS + 1])
async def test_shared_status_rejects_future_or_stale_source_proof(tmp_path: Path, age: int) -> None:
    source = MarketLivenessStore()
    source.mark_stream_connected(observed_at_ms=_NOW)
    paper = MarketLivenessStore()
    await _consumer(tmp_path, paper).refresh_clock()
    with pytest.raises(ValueError, match="stale or future"):
        paper.apply_status_snapshot(source.status_snapshot(now_ms=_NOW - age), now_ms=_NOW)
    assert paper.fact("SPY", now_ms=_NOW).state == "UNKNOWN"


@pytest.mark.parametrize("failure", ["timeout", "malformed", "unauthorized"])
async def test_shared_status_read_failure_immediately_invalidates_prior_proof(
    tmp_path: Path, failure: str,
) -> None:
    store = MarketLivenessStore()
    source = MarketLivenessStore()
    source.mark_stream_connected(observed_at_ms=_NOW)
    url = "http://status-owner:8000/api/brokers/alpaca/market-status-snapshot"
    journal = CaptureJournal(capture_dir=tmp_path / "capture", clock=lambda: _NOW)

    async def snapshot_source() -> MarketStatusSnapshot:
        return await read_shared_market_status(url, control_secret="test-control", journal=journal)

    consumer = AlpacaMarketLivenessConsumer(
        read=_Read(), frame_source=lambda: None,  # type: ignore[arg-type]
        status_snapshot_source=snapshot_source, store=store, clock=lambda: _NOW,
    )
    await consumer.refresh_clock()
    with respx.mock() as mock:
        route = mock.get(url).respond(200, json=source.status_snapshot(now_ms=_NOW).model_dump(mode="json"))
        await consumer.refresh_shared_status()
        assert store.fact("SPY", now_ms=_NOW).state == "TRADABLE"
        assert route.calls.last.request.headers["X-Data-Plane-Control-Secret"] == "test-control"
        assert "APCA-API-KEY-ID" not in route.calls.last.request.headers
        if failure == "timeout":
            route.mock(side_effect=httpx.ReadTimeout("test timeout"))
        elif failure == "malformed":
            route.respond(200, json={"connected":True})
        else:
            route.respond(403)
        await consumer.refresh_shared_status()
        assert store.fact("SPY", now_ms=_NOW).state == "UNKNOWN"


async def test_shared_status_endpoint_requires_auth_and_preserves_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings
    from app.routers.brokers import router

    store = MarketLivenessStore()
    primary = _consumer(tmp_path, store)
    primary.handle_frame(json.dumps([{"T":"s","S":"SPY","sc":"H","t":_iso_ms(_NOW)}]))
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_market_liveness_store] = lambda: store
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = "/api/brokers/alpaca/market-status-snapshot"
        assert (await client.get(path)).status_code == 403
        response = await client.get(path, headers={"X-Data-Plane-Control-Secret":"test-control"})
    assert response.status_code == 200
    snapshot = MarketStatusSnapshot.model_validate(response.json())
    assert snapshot.connected is True
    assert snapshot.symbol_statuses[0].state == "HALTED"
    assert snapshot.symbol_statuses[0].observed_at_ms == _NOW


async def test_production_factory_selects_the_configured_paper_status_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.broker.alpaca.config import AlpacaSettings
    from app.config import settings
    from app.utils.timestamps import now_ms_utc

    url = "http://status-owner:8000/api/brokers/alpaca/market-status-snapshot"
    source = MarketLivenessStore()
    source.mark_stream_connected(observed_at_ms=now_ms_utc())
    calls = []

    async def read_snapshot(actual_url: str, *, control_secret: str, journal: CaptureJournal):
        calls.append((actual_url, control_secret))
        return source.status_snapshot(now_ms=now_ms_utc())

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control")
    monkeypatch.setattr("app.broker.alpaca.market_liveness.read_shared_market_status", read_snapshot)
    store = MarketLivenessStore()
    consumer = AlpacaMarketLivenessConsumer.for_alpaca(
        read=_Read(),  # type: ignore[arg-type]
        settings=AlpacaSettings(api_key_id="paper-key", api_secret_key="paper-secret", mode="paper", market_status_upstream_url=url),
        store=store,
        journal=CaptureJournal(capture_dir=tmp_path / "capture"),
    )
    await consumer.refresh_shared_status()
    assert store.status_snapshot(now_ms=now_ms_utc()).connected is True
    assert calls == [(url, "test-control")]


def test_status_message_without_source_time_is_not_applied(tmp_path: Path) -> None:
    """A missing/unparsable ``t`` is unverifiable evidence and must not lift
    (or impose) a state transition (#1671)."""
    store = MarketLivenessStore()
    store.observe_clock(
        BrokerClockEvidence(
            broker="alpaca",
            is_open=True,
            vendor_timestamp_ms=_NOW,
            next_open_ms=None,
            next_close_ms=None,
            observed_at_ms=_NOW,
        )
    )
    store.observe_symbol_status(
        SymbolTradingStatusEvidence(
            symbol="SPY",
            state="HALTED",
            source="test.status",
            observed_at_ms=_NOW,
            source_timestamp_ms=_NOW,
        )
    )
    consumer = _consumer(tmp_path, store)

    consumer.handle_frame(json.dumps([{"T": "s", "S": "SPY", "sc": "T"}]))  # no "t"

    fact = store.fact("SPY", now_ms=_NOW)
    assert fact.state == "HALTED"
    assert fact.reason_code == "SYMBOL_HALTED"


def test_future_dated_resume_is_not_applied(tmp_path: Path) -> None:
    """A source time far ahead of receipt time is unverifiable (clock skew
    or a malformed frame) and must not lift a genuine halt (#1671). Positive
    (resume) evidence is refused outright rather than fail-closed-applied,
    since it becomes the ordering key that would otherwise mask a later
    legitimate halt — see test_future_dated_halt_is_still_applied_fail_closed
    for the opposite (negative-evidence) case, which is applied instead of
    dropped."""
    store = MarketLivenessStore()
    store.observe_clock(
        BrokerClockEvidence(
            broker="alpaca",
            is_open=True,
            vendor_timestamp_ms=_NOW,
            next_open_ms=None,
            next_close_ms=None,
            observed_at_ms=_NOW,
        )
    )
    store.observe_symbol_status(
        SymbolTradingStatusEvidence(
            symbol="SPY",
            state="HALTED",
            source="test.status",
            observed_at_ms=_NOW,
            source_timestamp_ms=_NOW,
        )
    )
    consumer = _consumer(tmp_path, store)

    consumer.handle_frame(json.dumps([{"T": "s", "S": "SPY", "sc": "T", "t": "2099-01-01T00:00:00Z"}]))

    fact = store.fact("SPY", now_ms=_NOW)
    assert fact.state == "HALTED"
    assert fact.reason_code == "SYMBOL_HALTED"


def test_future_dated_halt_is_still_applied_fail_closed(tmp_path: Path) -> None:
    """Regression: unlike a future-dated resume, a future-dated HALT must
    not simply be dropped — silently ignoring it would leave the symbol on
    whatever it last resolved to (TRADABLE, the default with no prior
    record), exposing new entries to a halt the vendor did report, just
    with an untrustworthy timestamp. It must still be applied fail-closed."""
    store = MarketLivenessStore()
    store.observe_clock(
        BrokerClockEvidence(
            broker="alpaca",
            is_open=True,
            vendor_timestamp_ms=_NOW,
            next_open_ms=None,
            next_close_ms=None,
            observed_at_ms=_NOW,
        )
    )
    consumer = _consumer(tmp_path, store)

    consumer.handle_frame(json.dumps([{"T": "s", "S": "SPY", "sc": "H", "t": "2099-01-01T00:00:00Z"}]))

    fact = store.fact("SPY", now_ms=_NOW)
    assert fact.state == "HALTED"
    assert fact.reason_code == "SYMBOL_HALTED"


def test_future_dated_halt_does_not_poison_ordering_for_a_later_legitimate_resume(
    tmp_path: Path,
) -> None:
    """Regression: applying a future-dated HALT must stamp it with the
    receipt clock, not the unverifiable claimed time — accepting the bogus
    future value verbatim as the ordering key would permanently mask any
    later legitimate resume for this symbol, the same poisoning risk a
    future-dated resume itself poses."""
    store = MarketLivenessStore()
    store.observe_clock(
        BrokerClockEvidence(
            broker="alpaca",
            is_open=True,
            vendor_timestamp_ms=_NOW,
            next_open_ms=None,
            next_close_ms=None,
            observed_at_ms=_NOW,
        )
    )
    consumer = _consumer(tmp_path, store)

    consumer.handle_frame(json.dumps([{"T": "s", "S": "SPY", "sc": "H", "t": "2099-01-01T00:00:00Z"}]))
    consumer.handle_frame(json.dumps([{"T": "s", "S": "SPY", "sc": "T", "t": "2023-11-14T22:13:25Z"}]))

    fact = store.fact("SPY", now_ms=_NOW)
    assert fact.state == "TRADABLE"
    assert fact.reason_code == "MARKET_TRADABLE"


def test_status_message_exactly_at_the_future_skew_boundary_is_applied(tmp_path: Path) -> None:
    """Pins the tolerance boundary itself: a resume dated exactly
    ``receipt_ms + _MAX_FUTURE_SKEW_MS`` (5,000ms) is within tolerance and
    must still lift a prior halt, not just anything strictly less than it."""
    store = MarketLivenessStore()
    store.observe_clock(
        BrokerClockEvidence(
            broker="alpaca",
            is_open=True,
            vendor_timestamp_ms=_NOW,
            next_open_ms=None,
            next_close_ms=None,
            observed_at_ms=_NOW,
        )
    )
    store.observe_symbol_status(
        SymbolTradingStatusEvidence(
            symbol="SPY",
            state="HALTED",
            source="test.status",
            observed_at_ms=_NOW,
            source_timestamp_ms=_NOW,
        )
    )
    consumer = _consumer(tmp_path, store)

    consumer.handle_frame(
        json.dumps([{"T": "s", "S": "SPY", "sc": "T", "t": _iso_ms(_NOW + 5_000)}])
    )

    fact = store.fact("SPY", now_ms=_NOW)
    assert fact.state == "TRADABLE"
    assert fact.reason_code == "MARKET_TRADABLE"


def test_status_message_one_ms_past_the_future_skew_boundary_is_refused(tmp_path: Path) -> None:
    """The complementary boundary case: one millisecond past tolerance, the
    same resume must be refused, leaving the prior halt in place."""
    store = MarketLivenessStore()
    store.observe_clock(
        BrokerClockEvidence(
            broker="alpaca",
            is_open=True,
            vendor_timestamp_ms=_NOW,
            next_open_ms=None,
            next_close_ms=None,
            observed_at_ms=_NOW,
        )
    )
    store.observe_symbol_status(
        SymbolTradingStatusEvidence(
            symbol="SPY",
            state="HALTED",
            source="test.status",
            observed_at_ms=_NOW,
            source_timestamp_ms=_NOW,
        )
    )
    consumer = _consumer(tmp_path, store)

    consumer.handle_frame(
        json.dumps([{"T": "s", "S": "SPY", "sc": "T", "t": _iso_ms(_NOW + 5_001)}])
    )

    fact = store.fact("SPY", now_ms=_NOW)
    assert fact.state == "HALTED"
    assert fact.reason_code == "SYMBOL_HALTED"


@pytest.mark.asyncio
async def test_halted_symbol_evidence_survives_a_stream_reconnect(tmp_path: Path) -> None:
    """Regression: Alpaca's status stream has no snapshot-on-subscribe and no
    REST fallback for current halt state (#1671 follow-up). A symbol proven
    HALTED before a disconnect must stay HALTED through a reconnect that
    produces no fresh transition for it — dropping that evidence just
    because the socket cycled would silently report it TRADABLE the instant
    any frame (for any symbol) arrives, which is the unsafe failure
    direction for a gate whose whole purpose is blocking new exposure into a
    real halt."""
    store = MarketLivenessStore()
    store.observe_clock(
        BrokerClockEvidence(
            broker="alpaca",
            is_open=True,
            vendor_timestamp_ms=_NOW,
            next_open_ms=None,
            next_close_ms=None,
            observed_at_ms=_NOW,
        )
    )
    attempts = [
        [json.dumps([{"T": "s", "S": "SPY", "sc": "H", "t": "2023-11-14T22:13:20Z"}])],
        [json.dumps([{"T": "s", "S": "AAPL", "sc": "T", "t": "2023-11-14T22:13:25Z"}])],
    ]
    calls = {"n": 0}
    captured: list[MarketLivenessFact] = []

    async def frame_source() -> AsyncIterator[str]:
        idx = calls["n"]
        calls["n"] += 1
        for frame in attempts[idx]:
            yield frame
        if idx == 1:
            # Still mid-stream on the reconnected socket cycle here — the
            # generator hasn't exited, so the connection watermark hasn't
            # flipped back to disconnected yet. Capture rather than assert:
            # an AssertionError here would be swallowed by _consume_statuses'
            # own broad except-Exception handler.
            captured.append(store.fact("SPY", now_ms=_NOW))
        raise RuntimeError("simulated disconnect")

    async def _noop_backoff(_attempt: int) -> None:
        return None

    consumer = AlpacaMarketLivenessConsumer(
        read=_Read(),  # type: ignore[arg-type]
        frame_source=frame_source,
        store=store,
        journal=CaptureJournal(capture_dir=tmp_path / "capture", clock=lambda: _NOW),
        clock=lambda: _NOW,
        backoff=_noop_backoff,
        max_reconnects=1,
    )

    await consumer._consume_statuses()

    assert len(captured) == 1
    fact = captured[0]
    assert fact.state == "HALTED"
    assert fact.reason_code == "SYMBOL_HALTED"


def test_future_dated_status_message_cannot_poison_ordering_for_later_events(tmp_path: Path) -> None:
    """Regression: source_timestamp_ms is the primary ordering key in
    observe_symbol_status's freshness guard. If a future-dated resume were
    ever accepted, every subsequent legitimate event — including a real,
    later halt — would look "older" by comparison and be silently
    discarded, leaving the symbol falsely TRADABLE forever."""
    store = MarketLivenessStore()
    store.observe_clock(
        BrokerClockEvidence(
            broker="alpaca",
            is_open=True,
            vendor_timestamp_ms=_NOW,
            next_open_ms=None,
            next_close_ms=None,
            observed_at_ms=_NOW,
        )
    )
    consumer = _consumer(tmp_path, store)

    # A clock-skewed or malformed resume claims a timestamp far in the future.
    consumer.handle_frame(json.dumps([{"T": "s", "S": "SPY", "sc": "T", "t": "2099-01-01T00:00:00Z"}]))
    # A real, later halt with an ordinary, current source time must still apply.
    consumer.handle_frame(json.dumps([{"T": "s", "S": "SPY", "sc": "H", "t": "2023-11-14T22:13:25Z"}]))

    fact = store.fact("SPY", now_ms=_NOW)
    assert fact.state == "HALTED"
    assert fact.reason_code == "SYMBOL_HALTED"
