"""Tests for GalleryHub snapshot composition (Task 2, bot gallery)."""

from __future__ import annotations

import asyncio

import pytest

from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.contract.models import OrderSide
from app.broker.v2panel.vocabulary import ActionId
from app.schemas.broker_v2_panel import PanelAction
from app.services.broker_v2_panel import gallery_hub
from app.services.broker_v2_panel.chart_projection_service import markers_in_window
from app.services.broker_v2_panel.gallery_hub import GalleryHub, shown_symbols


def _row_action(action_id: ActionId, *, enabled: bool, explanation: str = "") -> PanelAction:
    """A real ``PanelAction`` — the roster's authoritative per-row action
    ``GalleryHub._primary_action`` reuses when present (see that method's
    docstring for why enablement isn't unconditional for a stopped bot)."""
    return PanelAction(
        action_id=action_id,
        label=action_id,
        explanation=explanation,
        enabled=enabled,
        blockers=[],
        confirmation=None,
        revision=1,
        concurrency_token="tok-1",
    )

# A Saturday (market closed) so ``live_window`` takes its deterministic
# calendar-day fallback instead of a real NYSE session lookup — mirrors
# ``test_chart_projection.py``'s ``test_live_window_falls_back_when_market_closed``.
_NOW = 1_700_319_600_000
_OPEN_MS = 1_700_265_600_000
_CLOSE_MS = _OPEN_MS + 86_400_000


def _fill(
    *,
    sid: str,
    event_key: str,
    side: OrderSide = OrderSide.BUY,
    quantity: float = 10.0,
    price: float = 100.0,
    filled_at_ms: int,
) -> FillRecord:
    return FillRecord(
        account_id="PA3",
        sid=sid,
        intent_id="intent",
        order_ref=f"learn-ai/{sid}/v1:intent",
        event_key=event_key,
        symbol="SPY",
        side=side,
        quantity=quantity,
        fill_price=price,
        filled_at_ms=filled_at_ms,
        fee=None,
    )


class _FakeFillSource:
    """Mirrors the REAL ``GalleryFillSource`` contract (production:
    ``broker_v2_gallery._PanelChartFillSource``): returns ``(symbol, fills)``
    per sid and never raises. ``fills_by_sid`` is public and mutable so tests
    can simulate a new fill arriving between two calls."""

    def __init__(self, fills_by_sid: dict[str, tuple[FillRecord, ...]] | None = None) -> None:
        self.fills_by_sid: dict[str, tuple[FillRecord, ...]] = fills_by_sid or {}
        self.call_count = 0

    async def resolve_symbol_and_fills(
        self, broker: str, account_id: str, sid: str, *, now_ms: int
    ) -> tuple[str, tuple[FillRecord, ...]]:
        self.call_count += 1
        return "", self.fills_by_sid.get(sid, ())


def _status_label_for(*, phase: str, running: bool) -> str:
    """Mirrors ``catalog_projection_service.status_label_for`` so these
    fakes carry an accurate ``status_label`` — the field ``GalleryHub``
    actually reads (``_is_retired``), not the raw ``phase``."""
    if phase == "RETIRED":
        return "Retired"
    return "Working" if running else "Off duty"


class _Cat:
    def __init__(self, sid: str, symbol: str, running: bool, *, phase: str = "ON_DUTY") -> None:
        self.strategy_instance_id = sid
        self.symbol = symbol
        self.running = running
        self.phase = phase

    @property
    def status_label(self) -> str:
        # A property, not an init-time value: mirrors production, where a
        # freshly-fetched catalog row's status_label is always computed from
        # its current phase/running, never stale. Several tests mutate
        # ``.phase``/``.running`` in place mid-test to simulate a poll
        # observing a changed row — an init-time value would go stale there.
        return _status_label_for(phase=self.phase, running=self.running)


def test_shown_symbols_dedupes_across_running_and_stopped_bots() -> None:
    cat = [_Cat("a", "SPY", True), _Cat("b", "SPY", False), _Cat("c", "QQQ", True), _Cat("d", "IWM", False)]
    assert shown_symbols(cat) == ["SPY", "QQQ", "IWM"]


def test_shown_symbols_includes_symbol_held_only_by_a_stopped_bot() -> None:
    cat = [_Cat("a", "IWM", False)]
    assert shown_symbols(cat) == ["IWM"]


def test_shown_symbols_excludes_retired_bot_symbols() -> None:
    cat = [
        _Cat("a", "SPY", True, phase="ON_DUTY"),
        _Cat("b", "IWM", False, phase="RETIRED"),
    ]
    assert shown_symbols(cat) == ["SPY"]


class _Cat2:
    """Richer catalog stub mirroring the ``BotCatalogView`` fields GalleryHub reads."""

    def __init__(
        self,
        sid: str,
        symbol: str,
        running: bool,
        realized_pnl_today: float | None,
        open_pnl: float | None,
        fills_today: int | None,
        *,
        strategy_label: str = "",
        needs_attention: bool = False,
        phase: str = "ON_DUTY",
        row_action: PanelAction | None = None,
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
        self.row_action = row_action

    @property
    def status_label(self) -> str:
        # See ``_Cat.status_label`` for why this is a property, not an
        # init-time value.
        return _status_label_for(phase=self.phase, running=self.running)


class _FakeCatalogSource:
    def __init__(self, rows: list[_Cat2]) -> None:
        self._rows = rows
        self.call_count = 0

    async def get_catalog(self, broker: str, account_id: str) -> list[_Cat2]:
        self.call_count += 1
        return self._rows


def _raw_bar(**overrides: object) -> object:
    fields = {
        "start_ms": 1_700_000_000_000,
        "end_ms": 1_700_000_060_000,
        "open": 1.0,
        "high": 1.2,
        "low": 0.9,
        "close": 1.1,
        "volume": 100,
        "source": "ibkr",
        **overrides,
    }
    return type("B", (), fields)()


class _FakeAggregator:
    """Mirrors the REAL contract: ``ensure_subscribed`` is async, ``snapshot`` is sync.

    ``bars_by_symbol`` is public and mutable so a test can pre-configure a
    richer bar history (e.g. a prior-session bar followed by today's bars)
    for symbols it cares about; any symbol not configured falls back to the
    single default bar every existing test already relies on."""

    def __init__(self, bars_by_symbol: dict[str, list[object]] | None = None) -> None:
        self.subscribed: list[str] = []
        self.bars_by_symbol: dict[str, list[object]] = bars_by_symbol or {}

    async def ensure_subscribed(self, symbol: str) -> None:
        self.subscribed.append(symbol)

    def snapshot(self, symbol: str, since_ms: int | None = None) -> list[object]:
        bars = self.bars_by_symbol.get(symbol, [_raw_bar()])
        if since_ms is None:
            return bars
        return [bar for bar in bars if bar.start_ms > since_ms]


@pytest.mark.asyncio
async def test_build_snapshot_subscribes_once_per_symbol_and_projects_bots() -> None:
    rows = [
        _Cat2("Aug11-02", "SPY", True, 142.0, -8.0, 12),
        _Cat2("Aug11-03", "SPY", True, 10.0, 0.0, 3),
    ]
    aggregator = _FakeAggregator()
    hub = GalleryHub(
        broker="alpaca",
        account_id="PA3",
        catalog_source=_FakeCatalogSource(rows),
        aggregator=aggregator,
    )

    snap = await hub.build_snapshot()

    assert [s.symbol for s in snap.symbols] == ["SPY"]  # deduped
    assert {b.sid for b in snap.bots} == {"Aug11-02", "Aug11-03"}
    assert snap.surface_version == 1
    assert snap.resolution == "1m"
    assert aggregator.subscribed == ["SPY"]  # subscribed exactly once
    assert all(b.last_bar_at_ms == 1_700_000_060_000 for b in snap.bots)  # SPY's latest bar end_ms


@pytest.mark.asyncio
async def test_build_snapshot_includes_stopped_bot_with_resume_action_and_its_bars() -> None:
    """A stopped (non-retired) bot stays on the wall — projected with
    ``running=False`` and a Resume primary action — and its symbol's bars
    are fetched even though nothing running holds that symbol."""
    rows = [
        _Cat2("Aug11-02", "SPY", True, 142.0, -8.0, 12, phase="ON_DUTY"),
        _Cat2("Aug11-03", "QQQ", False, 5.0, 0.0, 1, phase="OFF_DUTY"),
    ]
    aggregator = _FakeAggregator()
    hub = GalleryHub(
        broker="alpaca",
        account_id="PA3",
        catalog_source=_FakeCatalogSource(rows),
        aggregator=aggregator,
    )

    snap = await hub.build_snapshot()

    assert {b.sid for b in snap.bots} == {"Aug11-02", "Aug11-03"}
    assert {s.symbol for s in snap.symbols} == {"SPY", "QQQ"}
    assert sorted(aggregator.subscribed) == ["QQQ", "SPY"]
    stopped = next(b for b in snap.bots if b.sid == "Aug11-03")
    assert stopped.running is False
    assert stopped.primary_action.action_id == "resume"
    assert stopped.primary_action.label == "Resume"


@pytest.mark.asyncio
async def test_build_snapshot_excludes_retired_bot_and_its_bars() -> None:
    rows = [
        _Cat2("Aug11-02", "SPY", True, 142.0, -8.0, 12, phase="ON_DUTY"),
        _Cat2("Aug11-04", "IWM", False, None, None, None, phase="RETIRED"),
    ]
    aggregator = _FakeAggregator()
    hub = GalleryHub(
        broker="alpaca",
        account_id="PA3",
        catalog_source=_FakeCatalogSource(rows),
        aggregator=aggregator,
    )

    snap = await hub.build_snapshot()

    assert {b.sid for b in snap.bots} == {"Aug11-02"}
    assert {s.symbol for s in snap.symbols} == {"SPY"}
    assert aggregator.subscribed == ["SPY"]  # retired bot's symbol never subscribed


class _EmptyAggregator(_FakeAggregator):
    """Same subscribe-tracking as ``_FakeAggregator``, but no bars for any symbol."""

    def snapshot(self, symbol: str, since_ms: int | None = None) -> list[object]:
        return []


@pytest.mark.asyncio
async def test_build_snapshot_last_bar_at_ms_none_when_symbol_has_no_bars() -> None:
    rows = [_Cat2("Aug11-02", "SPY", True, 142.0, -8.0, 12)]
    hub = GalleryHub(
        broker="alpaca",
        account_id="PA3",
        catalog_source=_FakeCatalogSource(rows),
        aggregator=_EmptyAggregator(),
    )

    snap = await hub.build_snapshot()

    assert snap.bots[0].last_bar_at_ms is None


@pytest.mark.asyncio
async def test_build_update_returns_only_new_bars_and_bumps_version() -> None:
    rows = [_Cat2("Aug11-02", "SPY", True, 142.0, -8.0, 12)]
    agg = _FakeAggregator()
    hub = GalleryHub(broker="alpaca", account_id="PA3", catalog_source=_FakeCatalogSource(rows), aggregator=agg)

    first = await hub.build_snapshot()
    upd = await hub.build_update(since_bar_ms={"SPY": 1_700_000_060_000}, known_sids={"Aug11-02"})

    assert upd.surface_version == first.surface_version + 1
    assert all(b.symbol == "SPY" for b in upd.symbols)
    assert upd.bots_delta[0].last_bar_at_ms == 1_700_000_060_000  # SPY's latest bar end_ms
    assert upd.bots_delta[0].sid == "Aug11-02"
    assert upd.markers_delta == {}
    assert upd.removed_sids == []


@pytest.mark.asyncio
async def test_build_update_stopped_bot_survives_not_removed() -> None:
    """A bot that stops between calls stays on the wall as a stopped
    (Resume) tile — only a bot that leaves the catalog entirely (retired or
    deleted) lands in ``removed_sids``, never one that merely stopped."""
    rows = [
        _Cat2("Aug11-02", "SPY", True, 142.0, -8.0, 12),
        _Cat2("Aug11-03", "SPY", True, 10.0, 0.0, 3),
    ]
    agg = _FakeAggregator()
    hub = GalleryHub(broker="alpaca", account_id="PA3", catalog_source=_FakeCatalogSource(rows), aggregator=agg)
    snapshot = await hub.build_snapshot()

    rows[1].running = False  # bot stops between snapshot and update

    upd = await hub.build_update(
        since_bar_ms={}, known_sids={b.sid for b in snapshot.bots}
    )

    assert upd.removed_sids == []
    assert {b.sid for b in upd.bots_delta} == {"Aug11-02", "Aug11-03"}
    stopped = next(b for b in upd.bots_delta if b.sid == "Aug11-03")
    assert stopped.running is False
    assert stopped.primary_action.action_id == "resume"


@pytest.mark.asyncio
async def test_build_update_removed_sids_when_bot_retires_between_calls() -> None:
    rows = [
        _Cat2("Aug11-02", "SPY", True, 142.0, -8.0, 12),
        _Cat2("Aug11-03", "SPY", True, 10.0, 0.0, 3),
    ]
    agg = _FakeAggregator()
    hub = GalleryHub(broker="alpaca", account_id="PA3", catalog_source=_FakeCatalogSource(rows), aggregator=agg)
    snapshot = await hub.build_snapshot()

    rows[1].phase = "RETIRED"  # bot retires between snapshot and update

    upd = await hub.build_update(
        since_bar_ms={}, known_sids={b.sid for b in snapshot.bots}
    )

    assert upd.removed_sids == ["Aug11-03"]
    assert {b.sid for b in upd.bots_delta} == {"Aug11-02"}


@pytest.mark.asyncio
async def test_build_update_removed_sids_when_bot_leaves_catalog_entirely() -> None:
    rows = [
        _Cat2("Aug11-02", "SPY", True, 142.0, -8.0, 12),
        _Cat2("Aug11-03", "SPY", True, 10.0, 0.0, 3),
    ]
    catalog_source = _FakeCatalogSource(rows)
    agg = _FakeAggregator()
    hub = GalleryHub(broker="alpaca", account_id="PA3", catalog_source=catalog_source, aggregator=agg)
    snapshot = await hub.build_snapshot()

    catalog_source._rows = [rows[0]]  # bot deleted from the catalog between calls

    upd = await hub.build_update(
        since_bar_ms={}, known_sids={b.sid for b in snapshot.bots}
    )

    assert upd.removed_sids == ["Aug11-03"]
    assert {b.sid for b in upd.bots_delta} == {"Aug11-02"}


@pytest.mark.asyncio
async def test_build_update_removed_sids_visible_to_every_independent_client() -> None:
    """Regression for the shared-hub-state bug: two SSE clients on the same
    account poll the same ``GalleryHub`` but must each track their own
    known-roster baseline (mirroring the router's per-stream ``known_sids``).
    Before the fix, a hub-wide baseline meant only the first caller ever saw
    a bot's removal."""
    rows = [
        _Cat2("Aug11-02", "SPY", True, 142.0, -8.0, 12),
        _Cat2("Aug11-03", "SPY", True, 10.0, 0.0, 3),
    ]
    agg = _FakeAggregator()
    hub = GalleryHub(broker="alpaca", account_id="PA3", catalog_source=_FakeCatalogSource(rows), aggregator=agg)
    snapshot = await hub.build_snapshot()
    baseline = {b.sid for b in snapshot.bots}

    rows[1].phase = "RETIRED"  # bot leaves the catalog before either client polls again

    client_a = await hub.build_update(since_bar_ms={}, known_sids=baseline)
    client_b = await hub.build_update(since_bar_ms={}, known_sids=baseline)

    assert client_a.removed_sids == ["Aug11-03"]
    assert client_b.removed_sids == ["Aug11-03"]


@pytest.mark.asyncio
async def test_build_update_preserves_last_bar_at_ms_when_no_new_bars() -> None:
    """A poll that observes no new bar for a symbol (the common case — bars
    close every 60s, polls fire every ~1s) must not blank out that symbol's
    already-known ``last_bar_at_ms``."""
    rows = [_Cat2("Aug11-02", "SPY", True, 142.0, -8.0, 12)]
    hub = GalleryHub(
        broker="alpaca",
        account_id="PA3",
        catalog_source=_FakeCatalogSource(rows),
        aggregator=_FakeAggregator(),
    )
    await hub.build_snapshot()

    hub._aggregator = _EmptyAggregator()  # this poll sees no new bar
    upd = await hub.build_update(since_bar_ms={"SPY": 1_700_000_060_000}, known_sids={"Aug11-02"})

    assert upd.bots_delta[0].last_bar_at_ms == 1_700_000_060_000


@pytest.mark.asyncio
async def test_build_snapshot_preserves_none_economics_instead_of_zero() -> None:
    """Unavailable catalog economics (``None``) must round-trip as ``None``,
    not a fabricated ``0`` — the frontend's dash formatters distinguish the
    two."""
    rows = [_Cat2("Aug11-02", "SPY", True, None, None, None)]
    hub = GalleryHub(
        broker="alpaca",
        account_id="PA3",
        catalog_source=_FakeCatalogSource(rows),
        aggregator=_FakeAggregator(),
    )

    snap = await hub.build_snapshot()

    bot = snap.bots[0]
    assert bot.realized_pnl_today is None
    assert bot.open_pnl is None
    assert bot.fills_today is None


@pytest.mark.asyncio
async def test_build_snapshot_session_change_pct_excludes_a_prior_session_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the aggregator's ring buffer retains slightly more than
    one session's worth of bars (``live_bar_aggregator.py``'s
    ``_RING_BUFFER_SIZE`` comment). ``session_change_pct`` must be computed
    from the first bar within TODAY's session, never from a naive
    ``bars[0]`` that can be yesterday's tail — a prior-session close-to-open
    gap must not leak into "today's" number."""
    monkeypatch.setattr(gallery_hub, "now_ms_utc", lambda: _NOW)
    prior_session_bar = _raw_bar(
        start_ms=_OPEN_MS - 3_600_000, end_ms=_OPEN_MS - 3_540_000, open=50.0, close=200.0,
    )
    todays_first_bar = _raw_bar(start_ms=_OPEN_MS, end_ms=_OPEN_MS + 60_000, open=100.0, close=101.0)
    todays_last_bar = _raw_bar(
        start_ms=_OPEN_MS + 60_000, end_ms=_OPEN_MS + 120_000, open=101.0, close=110.0,
    )
    rows = [_Cat2("Aug11-02", "SPY", True, 0.0, 0.0, 0)]
    hub = GalleryHub(
        broker="alpaca",
        account_id="PA3",
        catalog_source=_FakeCatalogSource(rows),
        aggregator=_FakeAggregator(
            {"SPY": [prior_session_bar, todays_first_bar, todays_last_bar]}
        ),
    )

    snap = await hub.build_snapshot()

    # (110 - 100) / 100 = 0.10 — NOT (110 - 50) / 50 = 1.2, which is what a
    # naive bars[0]-based calculation would have produced by folding in the
    # prior session's bar.
    assert snap.bots[0].session_change_pct == pytest.approx(0.10)


def test_gallery_hub_reuses_canonical_markers_projection() -> None:
    """``GalleryHub`` must not redefine fill→marker mapping — it imports the
    exact ``chart_projection_service`` helper the single-bot detail chart
    uses (CLAUDE.md single-source-of-truth rule), not a reimplementation."""
    assert gallery_hub.markers_in_window is markers_in_window


@pytest.mark.asyncio
async def test_build_snapshot_populates_markers_from_todays_fills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gallery_hub, "now_ms_utc", lambda: _NOW)
    rows = [_Cat2("Aug11-02", "SPY", True, 142.0, -8.0, 12)]
    in_window = _fill(
        sid="Aug11-02",
        event_key="exec-in",
        side=OrderSide.BUY,
        quantity=10,
        price=101.5,
        filled_at_ms=_OPEN_MS + 60_000,
    )
    out_of_window = _fill(
        sid="Aug11-02",
        event_key="exec-out",
        side=OrderSide.SELL,
        quantity=5,
        price=99.0,
        filled_at_ms=_OPEN_MS - 60_000,
    )
    fill_source = _FakeFillSource({"Aug11-02": (in_window, out_of_window)})
    hub = GalleryHub(
        broker="alpaca",
        account_id="PA3",
        catalog_source=_FakeCatalogSource(rows),
        aggregator=_FakeAggregator(),
        fill_source=fill_source,
    )

    snap = await hub.build_snapshot()

    # Byte-identical to calling the canonical helper directly — proves the
    # hub reused it rather than re-deriving an equivalent-looking mapping.
    expected = markers_in_window((in_window, out_of_window), from_ms=_OPEN_MS, to_ms=_CLOSE_MS)
    assert snap.markers == {"Aug11-02": expected}
    marker = snap.markers["Aug11-02"][0]
    assert marker.side == "buy"
    assert marker.quantity == 10
    assert marker.price == 101.5
    assert marker.filled_at_ms == in_window.filled_at_ms
    assert marker.order_ref == in_window.order_ref


@pytest.mark.asyncio
async def test_build_snapshot_bot_with_no_fills_omitted_from_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No fills in today's window → the sid is absent from ``markers``
    entirely, mirroring how ``last_bar_at_ms``'s backing map omits a symbol
    with no bars rather than storing an empty list under the key."""
    monkeypatch.setattr(gallery_hub, "now_ms_utc", lambda: _NOW)
    rows = [_Cat2("Aug11-02", "SPY", True, 142.0, -8.0, 12)]
    hub = GalleryHub(
        broker="alpaca",
        account_id="PA3",
        catalog_source=_FakeCatalogSource(rows),
        aggregator=_FakeAggregator(),
        fill_source=_FakeFillSource({"Aug11-02": ()}),
    )

    snap = await hub.build_snapshot()

    assert snap.markers == {}


@pytest.mark.asyncio
async def test_build_snapshot_markers_empty_without_fill_source() -> None:
    """No ``fill_source`` injected (e.g. bar/catalog-only tests) preserves
    this module's prior hard-coded ``markers={}`` behavior."""
    rows = [_Cat2("Aug11-02", "SPY", True, 142.0, -8.0, 12)]
    hub = GalleryHub(
        broker="alpaca",
        account_id="PA3",
        catalog_source=_FakeCatalogSource(rows),
        aggregator=_FakeAggregator(),
    )

    snap = await hub.build_snapshot()

    assert snap.markers == {}


@pytest.mark.asyncio
async def test_build_update_returns_new_fill_in_markers_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fill that lands after the snapshot shows up in the next poll's
    ``markers_delta``."""
    monkeypatch.setattr(gallery_hub, "now_ms_utc", lambda: _NOW)
    rows = [_Cat2("Aug11-02", "SPY", True, 142.0, -8.0, 12)]
    fill_source = _FakeFillSource({"Aug11-02": ()})
    hub = GalleryHub(
        broker="alpaca",
        account_id="PA3",
        catalog_source=_FakeCatalogSource(rows),
        aggregator=_FakeAggregator(),
        fill_source=fill_source,
    )
    snap = await hub.build_snapshot()
    assert snap.markers == {}

    new_fill = _fill(sid="Aug11-02", event_key="exec-new", filled_at_ms=_OPEN_MS + 120_000)
    fill_source.fills_by_sid["Aug11-02"] = (new_fill,)

    upd = await hub.build_update(since_bar_ms={}, known_sids={"Aug11-02"}, since_marker_keys={})

    assert [m.event_key for m in upd.markers_delta["Aug11-02"]] == [new_fill.event_key]


@pytest.mark.asyncio
async def test_build_update_does_not_resend_already_delivered_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fill already delivered (its ``event_key`` is in the caller's
    ``since_marker_keys`` cursor) is not resent in a later ``markers_delta``."""
    monkeypatch.setattr(gallery_hub, "now_ms_utc", lambda: _NOW)
    rows = [_Cat2("Aug11-02", "SPY", True, 142.0, -8.0, 12)]
    fill = _fill(sid="Aug11-02", event_key="exec-1", filled_at_ms=_OPEN_MS + 60_000)
    fill_source = _FakeFillSource({"Aug11-02": (fill,)})
    hub = GalleryHub(
        broker="alpaca",
        account_id="PA3",
        catalog_source=_FakeCatalogSource(rows),
        aggregator=_FakeAggregator(),
        fill_source=fill_source,
    )
    snap = await hub.build_snapshot()
    assert [m.event_key for m in snap.markers["Aug11-02"]] == [fill.event_key]

    # The caller (mirroring the router's ``since_marker_keys``) already saw
    # this fill in the snapshot, so its cursor is seeded with its event_key.
    upd = await hub.build_update(
        since_bar_ms={},
        known_sids={"Aug11-02"},
        since_marker_keys={"Aug11-02": {fill.event_key}},
    )

    assert "Aug11-02" not in upd.markers_delta


@pytest.mark.asyncio
async def test_build_update_does_not_drop_a_same_millisecond_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a strict ``filled_at_ms >`` watermark would silently and
    PERMANENTLY drop a second, genuinely distinct fill sharing the same
    millisecond as an already-delivered one — neither fill is ever ``>`` the
    other's timestamp. Keying on ``event_key`` instead must deliver both."""
    monkeypatch.setattr(gallery_hub, "now_ms_utc", lambda: _NOW)
    rows = [_Cat2("Aug11-02", "SPY", True, 142.0, -8.0, 12)]
    same_ms = _OPEN_MS + 60_000
    first = _fill(sid="Aug11-02", event_key="exec-1", filled_at_ms=same_ms)
    fill_source = _FakeFillSource({"Aug11-02": (first,)})
    hub = GalleryHub(
        broker="alpaca",
        account_id="PA3",
        catalog_source=_FakeCatalogSource(rows),
        aggregator=_FakeAggregator(),
        fill_source=fill_source,
    )
    snap = await hub.build_snapshot()
    assert [m.event_key for m in snap.markers["Aug11-02"]] == ["exec-1"]

    # A second, distinct fill arrives at the SAME millisecond as the one
    # already delivered.
    second = _fill(sid="Aug11-02", event_key="exec-2", filled_at_ms=same_ms)
    fill_source.fills_by_sid["Aug11-02"] = (first, second)

    upd = await hub.build_update(
        since_bar_ms={}, known_sids={"Aug11-02"}, since_marker_keys={"Aug11-02": {"exec-1"}}
    )

    assert [m.event_key for m in upd.markers_delta["Aug11-02"]] == ["exec-2"]


def _hub_with_rows(rows: list[_Cat2]) -> GalleryHub:
    return GalleryHub(
        broker="alpaca",
        account_id="PA3",
        catalog_source=_FakeCatalogSource(rows),
        aggregator=_FakeAggregator(),
    )


@pytest.mark.asyncio
async def test_primary_action_honors_disabled_row_action_for_stopped_bot() -> None:
    """A stopped bot isn't unconditionally Resume-able — when the roster's
    own authoritative row_action says Resume is blocked (e.g. admission
    unavailable), the gallery's primary_action must say so too, not present
    an enabled Resume the confirm-time authoritative-panel check then
    rejects."""
    row = _Cat2(
        "Aug11-02", "SPY", False, None, None, None,
        row_action=_row_action("resume", enabled=False, explanation="Admission unavailable."),
    )
    snap = await _hub_with_rows([row]).build_snapshot()

    action = snap.bots[0].primary_action
    assert action.action_id == "resume"
    assert action.enabled is False
    assert action.disabled_reason == "Admission unavailable."


@pytest.mark.asyncio
async def test_primary_action_uses_row_action_enabled_true_for_running_bot() -> None:
    row = _Cat2(
        "Aug11-02", "SPY", True, None, None, None,
        row_action=_row_action("stop", enabled=True, explanation="Stop this bot."),
    )
    snap = await _hub_with_rows([row]).build_snapshot()

    action = snap.bots[0].primary_action
    assert action.action_id == "stop"
    assert action.enabled is True
    assert action.disabled_reason is None


@pytest.mark.asyncio
async def test_primary_action_falls_back_when_row_action_names_a_different_action() -> None:
    """The roster's routine action for this row isn't stop/resume (e.g. a
    hold needs clearing first) — the gallery's one quick-action slot can't
    represent that yet, so it falls back to the pre-fix always-enabled
    derivation rather than mis-attributing an unrelated action's enablement."""
    row = _Cat2(
        "Aug11-02", "SPY", False, None, None, None,
        row_action=_row_action("clear_hold", enabled=False, explanation="A hold is active."),
    )
    snap = await _hub_with_rows([row]).build_snapshot()

    action = snap.bots[0].primary_action
    assert action.action_id == "resume"
    assert action.enabled is True
    assert action.disabled_reason is None


@pytest.mark.asyncio
async def test_primary_action_falls_back_when_no_row_action_projected() -> None:
    """Backward compat: a catalog source that doesn't populate row_action
    (most test doubles, and any future non-SQLite source) keeps today's
    always-enabled derivation."""
    row = _Cat2("Aug11-02", "SPY", False, None, None, None)  # row_action=None
    snap = await _hub_with_rows([row]).build_snapshot()

    action = snap.bots[0].primary_action
    assert action.action_id == "resume"
    assert action.enabled is True


@pytest.mark.asyncio
async def test_io_cache_collapses_concurrent_client_catalog_and_fill_fetches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: N SSE clients independently polling the SAME cached hub
    must not each trigger their own catalog fetch and fill-source fan-out —
    within the TTL window, they share one of each
    (``GalleryHub.__init__``'s ``io_cache_ttl_ms`` docstring)."""
    clock = {"now": _NOW}
    monkeypatch.setattr(gallery_hub, "now_ms_utc", lambda: clock["now"])
    rows = [_Cat2("Aug11-02", "SPY", True, 0.0, 0.0, 0)]
    catalog_source = _FakeCatalogSource(rows)
    fill_source = _FakeFillSource()
    hub = GalleryHub(
        broker="alpaca",
        account_id="PA3",
        catalog_source=catalog_source,
        aggregator=_FakeAggregator(),
        fill_source=fill_source,
        io_cache_ttl_ms=1_000,
    )

    # Three "clients" polling this shared hub within the same TTL window.
    await hub.build_snapshot()
    await hub.build_update(since_bar_ms={}, known_sids={"Aug11-02"})
    await hub.build_update(since_bar_ms={}, known_sids={"Aug11-02"})

    assert catalog_source.call_count == 1
    assert fill_source.call_count == 1  # one shown bot, one fan-out call

    # Advance past the TTL — the next poll must fetch fresh.
    clock["now"] += 1_001
    await hub.build_update(since_bar_ms={}, known_sids={"Aug11-02"})

    assert catalog_source.call_count == 2
    assert fill_source.call_count == 2


@pytest.mark.asyncio
async def test_io_cache_disabled_by_default_fetches_fresh_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``io_cache_ttl_ms`` defaults to ``0`` — caching must be fully
    disabled unless a caller opts in, exactly the pre-caching behavior every
    other test in this file depends on."""
    monkeypatch.setattr(gallery_hub, "now_ms_utc", lambda: _NOW)
    rows = [_Cat2("Aug11-02", "SPY", True, 0.0, 0.0, 0)]
    catalog_source = _FakeCatalogSource(rows)
    hub = GalleryHub(
        broker="alpaca",
        account_id="PA3",
        catalog_source=catalog_source,
        aggregator=_FakeAggregator(),
    )

    await hub.build_snapshot()
    await hub.build_snapshot()

    assert catalog_source.call_count == 2


class _BlockingFillSource:
    """Blocks only the FIRST call into ``resolve_symbol_and_fills`` until
    ``release`` is set; every call after that returns immediately —
    regardless of which sid it's for, so this works even when both
    concurrent ``GalleryHub`` calls share the same single bot (as they do in
    ``test_build_update_version_not_stolen_by_a_concurrent_call``: gating on
    a specific sid would block BOTH calls, since both resolve the same bot).
    Deterministically makes whichever call enters ``_fetch_markers`` first
    the "slow" one (mirrors production: one hub cached per account, called
    by every concurrent client)."""

    def __init__(self, *, release: asyncio.Event) -> None:
        self._release = release
        self._first_call_started = False

    async def resolve_symbol_and_fills(
        self, broker: str, account_id: str, sid: str, *, now_ms: int
    ) -> tuple[str, tuple[FillRecord, ...]]:
        if not self._first_call_started:
            self._first_call_started = True
            await self._release.wait()
        return "", ()


@pytest.mark.asyncio
async def test_build_update_version_not_stolen_by_a_concurrent_call() -> None:
    """Regression: ``surface_version`` must reflect THIS call's own claimed
    version even when another concurrent call (same cached hub, a second SSE
    client or the REST fallback poll) increments ``self._version`` again
    while this call is still awaiting ``_fetch_markers``. Before the fix,
    ``surface_version`` was read from ``self._version`` at return-construction
    time — after the await — so the slower call would silently inherit the
    faster call's later version number."""
    release = asyncio.Event()
    rows = [_Cat2("Aug11-02", "SPY", True, 142.0, -8.0, 12)]
    hub = GalleryHub(
        broker="alpaca",
        account_id="PA3",
        catalog_source=_FakeCatalogSource(rows),
        aggregator=_FakeAggregator(),
        fill_source=_BlockingFillSource(release=release),
    )

    slow_call = asyncio.ensure_future(hub.build_update(since_bar_ms={}, known_sids=set()))
    await asyncio.sleep(0)  # let the slow call increment its version and start waiting

    fast_result = await hub.build_update(since_bar_ms={}, known_sids=set())
    release.set()
    slow_result = await slow_call

    assert slow_result.surface_version == 1
    assert fast_result.surface_version == 2
