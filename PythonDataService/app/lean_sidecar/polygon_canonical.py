"""Canonical Polygon minute-bar source for the LEAN sidecar.

The provider Protocol lets the production orchestrator and tests share
the same fetch contract while sourcing bars from different places.
Tests inject ``RecordedPolygonFixtureProvider``; production injects
``PolygonProvider``. ``fetch_canonical_minute_bars`` (Task 3) applies
the RTH/extended filter and fail-fast monotonicity + dedup checks.

See docs/superpowers/specs/2026-05-19-lean-engine-polygon-parity-design.md.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from app.engine.data.polygon_export import group_by_trading_date, polygon_bar_to_trade_bar
from app.engine.data.trade_bar import TradeBar
from app.services.dataset_service import fetch_bars_chunks_raw
from app.services.polygon_client import PolygonClientService


class CanonicalBarsProvider(Protocol):
    """Source of raw 1-minute Polygon-style bars for a single symbol."""

    # Identity properties — recorded in the manifest's ``data_policy``
    # so a run's bars are pinned to either "live Polygon as of
    # captured_at" or to a specific captured fixture by content sha.
    @property
    def fixture_id(self) -> str | None:
        """Stable name of the fixture replayed (e.g., dir name), or None for live."""
        ...

    @property
    def fixture_sha256(self) -> str | None:
        """sha256 of the fixture's bars.json bytes, or None for live."""
        ...

    def fetch_minute_bars(
        self,
        *,
        symbol: str,
        start_date: date,
        end_date: date,
        adjusted: bool,
    ) -> list[dict[str, Any]]:
        """Return bar dicts: ``timestamp`` (ms UTC, start-of-bar), ``open``, ``high``, ``low``, ``close``, ``volume``."""
        ...


class FixtureMetadataMismatchError(ValueError):
    """The (symbol, range, adjusted) tuple does not match the fixture's metadata.json.

    Raised by ``RecordedPolygonFixtureProvider`` to prevent a test from
    silently loading the wrong window when its request shape drifts
    from what the fixture was captured for.
    """


@dataclass(frozen=True, slots=True)
class RecordedPolygonFixtureProvider:
    """Replays a captured Polygon fetch from a fixture directory.

    The fixture directory contains ``bars.json`` (list of bar dicts),
    ``metadata.json`` (machine-readable manifest the provider asserts
    against), and a human ``attribution.md`` (not parsed).

    Metadata is loaded once at construction so the manifest-provenance
    properties (:attr:`fixture_id`, :attr:`fixture_sha256`) are cheap
    to read and a malformed metadata.json fails at instantiation rather
    than first fetch. ``bars.json`` itself is hashed and compared to
    ``metadata.bars_sha256`` on every fetch — silent in-place edits to
    the captured bars are rejected as ``FixtureMetadataMismatchError``.
    """

    fixture_dir: Path
    _meta: dict[str, Any] = field(init=False)

    def __post_init__(self) -> None:
        meta = json.loads((self.fixture_dir / "metadata.json").read_text())
        if meta.get("schema_version") != 1:
            raise FixtureMetadataMismatchError(
                f"fixture {self.fixture_dir.name!r} has metadata schema_version="
                f"{meta.get('schema_version')!r}; this provider supports only schema_version=1"
            )
        # ``frozen=True`` blocks plain attribute assignment, but the
        # cached metadata is part of the object's identity-by-content.
        # Use object.__setattr__ to populate the slot exactly once.
        object.__setattr__(self, "_meta", meta)

    @property
    def fixture_id(self) -> str | None:
        return self.fixture_dir.name

    @property
    def fixture_sha256(self) -> str | None:
        return self._meta.get("bars_sha256")

    def fetch_minute_bars(
        self,
        *,
        symbol: str,
        start_date: date,
        end_date: date,
        adjusted: bool,
    ) -> list[dict[str, Any]]:
        meta = self._meta
        expected = (
            ("symbol", meta["symbol"], symbol),
            ("from_date", meta["from_date"], start_date.isoformat()),
            ("to_date", meta["to_date"], end_date.isoformat()),
            ("adjusted", meta["adjusted"], adjusted),
        )
        mismatches = [
            (mfield, fixture_val, asked_val) for mfield, fixture_val, asked_val in expected if fixture_val != asked_val
        ]
        if mismatches:
            details = "; ".join(
                f"{mfield}: fixture={fixture_val!r} asked={asked_val!r}"
                for mfield, fixture_val, asked_val in mismatches
            )
            raise FixtureMetadataMismatchError(f"fixture {self.fixture_dir.name!r} does not match request: {details}")
        # Hash the bytes (not the parsed JSON) so re-serialization
        # quirks (whitespace, key order) cannot mask tampering. Match
        # against metadata.bars_sha256 — silent in-place edits to
        # bars.json fail loud here.
        bars_path = self.fixture_dir / "bars.json"
        bars_bytes = bars_path.read_bytes()
        expected_sha = meta.get("bars_sha256")
        if expected_sha is not None:
            actual_sha = hashlib.sha256(bars_bytes).hexdigest()
            if actual_sha != expected_sha:
                raise FixtureMetadataMismatchError(
                    f"fixture {self.fixture_dir.name!r} bars.json sha256={actual_sha[:12]}... "
                    f"does not match metadata.bars_sha256={expected_sha[:12]}... — "
                    "fixture was edited in place; regenerate via scripts/regenerate_polygon_fixture.py"
                )
        bars: list[dict[str, Any]] = json.loads(bars_bytes)
        return bars


@dataclass(frozen=True, slots=True)
class PolygonProvider:
    """Live Polygon fetch via the existing chunked aggregator.

    Always requests 1-minute bars at multiplier 1 — strategy timeframes
    are produced by per-engine consolidation, not by Polygon-native
    aggregates. See spec §"Polygon data source".
    """

    polygon: PolygonClientService

    @property
    def fixture_id(self) -> str | None:
        """Live provider has no fixture identity."""
        return None

    @property
    def fixture_sha256(self) -> str | None:
        """Live provider has no fixture identity."""
        return None

    def fetch_minute_bars(
        self,
        *,
        symbol: str,
        start_date: date,
        end_date: date,
        adjusted: bool,
    ) -> list[dict[str, Any]]:
        # Raw path: the strict monotonicity / uniqueness check in
        # ``fetch_canonical_minute_bars`` is the canonical-input
        # boundary. ``fetch_bars_chunked`` would silently dedupe + re-sort
        # before that check ever fires, masking upstream Polygon
        # corruption.
        return fetch_bars_chunks_raw(
            polygon=self.polygon,
            ticker=symbol,
            from_date=start_date.isoformat(),
            to_date=end_date.isoformat(),
            timespan="minute",
            multiplier=1,
            adjusted=adjusted,
        )


def get_default_provider() -> CanonicalBarsProvider:
    """Construct the default production provider.

    Tests monkey-patch this function to inject a
    ``RecordedPolygonFixtureProvider``. The orchestrator
    (``run_trusted_sample``) calls this once per Polygon-source run so a
    monkey-patch at module scope is enough — no need to thread a
    provider parameter through the FastAPI router.
    """
    return PolygonProvider(polygon=PolygonClientService())


_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")

# RTH session: [09:30, 16:00) ET.
_RTH_OPEN_MINUTE = 9 * 60 + 30
_RTH_CLOSE_MINUTE = 16 * 60


class CanonicalBarsError(ValueError):
    """Polygon returned bars that violate the canonical-input contract.

    Per .claude/rules/numerical-rigor.md § "External-API ingestion",
    duplicates and non-monotonic timestamps must surface as errors,
    not be silently repaired.
    """


def _is_rth(ts_ms: int) -> bool:
    et = datetime.fromtimestamp(ts_ms / 1000, tz=_UTC).astimezone(_ET)
    minute_of_day = et.hour * 60 + et.minute
    return _RTH_OPEN_MINUTE <= minute_of_day < _RTH_CLOSE_MINUTE


def fetch_canonical_minute_bars(
    *,
    symbol: str,
    start_date: date,
    end_date: date,
    session: Literal["regular", "extended"],
    adjustment: Literal["raw"],
    provider: CanonicalBarsProvider,
) -> list[tuple[date, list[TradeBar]]]:
    """Fetch Polygon 1-minute bars, filter by session, group by ET trading date.

    Fail-fast on duplicate or non-monotonic timestamps — per the
    numerical-rigor rule, such bars are signals about upstream
    corruption and must surface, not be silently dropped.
    """
    if adjustment != "raw":
        raise ValueError(f"only adjustment='raw' supported in Phase 1, got {adjustment!r}")

    raw = provider.fetch_minute_bars(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        adjusted=False,  # adjustment=="raw" ⇔ adjusted=False
    )

    # Fail-fast validation: strict monotonic, no duplicates. This is
    # the production canonical-input enforcement path; ``PolygonProvider``
    # routes through ``fetch_bars_chunks_raw`` precisely so this loop —
    # not a silent re-sort in ``fetch_bars_chunked`` — sees the raw wire
    # bars.
    prev_ts: int | None = None
    seen: set[int] = set()
    for bar in raw:
        ts = int(bar["timestamp"])
        if ts in seen:
            et_repr = datetime.fromtimestamp(ts / 1000, tz=_UTC).astimezone(_ET).isoformat()
            raise CanonicalBarsError(f"polygon_corrupt_timestamps: duplicate timestamp {ts} ({et_repr}) for {symbol}")
        if prev_ts is not None and ts <= prev_ts:
            et_repr = datetime.fromtimestamp(ts / 1000, tz=_UTC).astimezone(_ET).isoformat()
            prev_et = datetime.fromtimestamp(prev_ts / 1000, tz=_UTC).astimezone(_ET).isoformat()
            raise CanonicalBarsError(
                f"polygon_corrupt_timestamps: non-monotonic timestamp {ts} ({et_repr}) "
                f"after {prev_ts} ({prev_et}) for {symbol}"
            )
        seen.add(ts)
        prev_ts = ts

    # Session filter.
    if session == "regular":
        filtered = [b for b in raw if _is_rth(int(b["timestamp"]))]
    else:
        filtered = list(raw)

    # Convert + group by ET trading date.
    trade_bars = [polygon_bar_to_trade_bar(symbol, bar) for bar in filtered]
    grouped = group_by_trading_date(trade_bars)

    # Window-completeness checks (regular session only — extended hours
    # legitimately have gappy fills outside RTH). Run *after* the
    # canonical fail-fast above so corrupt timestamps surface first.
    if session == "regular":
        _assert_window_complete(grouped=grouped, start_date=start_date, end_date=end_date, symbol=symbol)

    return [(d, grouped[d]) for d in sorted(grouped.keys())]


# Set of valid 09:30 + close-boundary minutes-of-day for boundary checks.
# A regular session closes at 16:00 ET; the last *bar* before close is
# 15:59 (start-of-bar minute). Half-days close earlier — historically
# almost always 13:00, so the last bar is 12:59.
_RTH_FIRST_BAR_MINUTE = _RTH_OPEN_MINUTE  # 09:30


def _assert_window_complete(
    *,
    grouped: dict[date, list[TradeBar]],
    start_date: date,
    end_date: date,
    symbol: str,
) -> None:
    """Fail-fast when Polygon returns an incomplete window.

    Two checks:
      1. Every NYSE session in ``[start_date, end_date]`` has at least
         one bar in ``grouped`` (a silently-dropped session would
         otherwise let both engines agree on partial data, producing a
         false-positive parity result).
      2. Every full session has the 09:30 ET and 15:59 ET boundary
         bars; every half-day session has 09:30 ET and the bar one
         minute before its half-day close (typically 12:59 ET).

    See .claude/rules/numerical-rigor.md § "External-API ingestion".
    """
    from app.lean_sidecar.trading_calendar import expected_sessions, session_close_minute_et

    expected = expected_sessions(start_date, end_date)
    missing_sessions = [d for d in expected if d not in grouped]
    if missing_sessions:
        names = ", ".join(d.isoformat() for d in missing_sessions)
        raise CanonicalBarsError(
            f"polygon_window_incomplete: missing sessions {names} between "
            f"{start_date.isoformat()} and {end_date.isoformat()} for {symbol}"
        )

    for d in expected:
        bars = grouped[d]
        # Derive each bar's start-of-bar minute-of-day in ET. Polygon
        # timestamps are start-of-bar; the canonical bar at 09:30 ET
        # is the first RTH bar.
        bar_minutes: set[int] = set()
        for tb in bars:
            et_dt = tb.time.astimezone(_ET)
            bar_minutes.add(et_dt.hour * 60 + et_dt.minute)

        close_minute = session_close_minute_et(d)
        # Last bar's start minute is close_minute - 1 (e.g., 15:59 for a
        # 16:00 close, 12:59 for a 13:00 close).
        required_last_bar_minute = close_minute - 1
        missing_boundaries: list[str] = []
        if _RTH_FIRST_BAR_MINUTE not in bar_minutes:
            missing_boundaries.append("09:30")
        if required_last_bar_minute not in bar_minutes:
            hh, mm = divmod(required_last_bar_minute, 60)
            missing_boundaries.append(f"{hh:02d}:{mm:02d}")
        if missing_boundaries:
            raise CanonicalBarsError(
                f"polygon_session_incomplete: session {d.isoformat()} missing boundary "
                f"bar(s) {missing_boundaries} for {symbol}"
            )
