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
from app.lean_sidecar.trading_calendar import is_regular_session_ms_utc
from app.services.dataset_service import (
    CanonicalBarsError,
    assert_canonical_bar_stream,
    fetch_bars_chunks_raw,
)
from app.services.polygon_client import PolygonClientService

__all__ = [
    "CanonicalBarsError",
    "CanonicalBarsProvider",
    "FixtureMetadataMismatchError",
    "PolygonProvider",
    "RecordedPolygonFixtureProvider",
    "fetch_canonical_minute_bars",
    "get_default_provider",
]


class CanonicalBarsProvider(Protocol):
    """Source of raw 1-minute Polygon-style bars for a single symbol.

    Implementations expose ``fixture_id`` and ``fixture_sha256`` as
    identity properties so the manifest's ``data_policy`` block can
    distinguish live-Polygon runs (both ``None``) from fixture replays.
    """

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


def _compact_sha256(bars: list[dict[str, Any]]) -> str:
    """Canonical sha256 over a bar list — same formula as the freshness canary.

    The committed ``metadata.bars_sha256`` is computed against
    ``json.dumps(bars, separators=(",", ":"))``; both the
    ``RecordedPolygonFixtureProvider`` post-load check and the
    ``test_polygon_fixture_matches_live_refetch`` canary must use the
    same formula so the two paths stay byte-equivalent.
    """
    canonical = json.dumps(bars, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class RecordedPolygonFixtureProvider:
    """Replays a captured Polygon fetch from a fixture directory.

    The fixture directory contains ``bars.json`` (list of bar dicts),
    ``metadata.json`` (machine-readable manifest the provider asserts
    against), and a human ``attribution.md`` (not parsed).

    Metadata is loaded once at construction so identity properties
    (:attr:`fixture_id`, :attr:`fixture_sha256`) are cheap and a
    malformed metadata.json fails at instantiation rather than first
    fetch. ``bars.json`` is verified against ``metadata.bars_sha256``
    on every fetch — silent in-place edits surface as
    ``FixtureMetadataMismatchError``.
    """

    fixture_dir: Path
    _meta: dict[str, Any] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        meta = json.loads((self.fixture_dir / "metadata.json").read_text())
        if meta.get("schema_version") != 1:
            raise FixtureMetadataMismatchError(
                f"fixture {self.fixture_dir.name!r} has metadata schema_version="
                f"{meta.get('schema_version')!r}; this provider supports only schema_version=1"
            )
        # frozen=True blocks plain assignment; use object.__setattr__ to
        # cache the parsed metadata exactly once.
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
        bars: list[dict[str, Any]] = json.loads((self.fixture_dir / "bars.json").read_text())
        # Verify the captured bars.json hasn't drifted from metadata.bars_sha256.
        # Skip the check when metadata declares no sha (legacy fixtures).
        expected_sha = meta.get("bars_sha256")
        if expected_sha is not None:
            actual_sha = _compact_sha256(bars)
            if actual_sha != expected_sha:
                raise FixtureMetadataMismatchError(
                    f"fixture {self.fixture_dir.name!r} bars.json sha256={actual_sha[:12]}... "
                    f"does not match metadata.bars_sha256={expected_sha[:12]}... — "
                    "fixture was edited in place; regenerate via scripts/regenerate_polygon_fixture.py"
                )
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
        # P1-DEDUP: use the raw (non-sanitizing) chunked path so that
        # duplicates / non-monotonic timestamps from Polygon surface to
        # the canonical-input fail-fast loop in
        # ``fetch_canonical_minute_bars`` rather than being silently
        # repaired in transit. See ``.claude/rules/numerical-rigor.md``
        # §"External-API ingestion".
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


def _is_rth(ts_ms: int) -> bool:
    return is_regular_session_ms_utc(ts_ms)


def fetch_canonical_minute_bars(
    *,
    symbol: str,
    start_date: date,
    end_date: date,
    session: Literal["regular", "extended"],
    adjustment: Literal["raw"],
    provider: CanonicalBarsProvider,
    strict_completeness: bool = False,
) -> list[tuple[date, list[TradeBar]]]:
    """Fetch Polygon 1-minute bars, filter by session, group by ET trading date.

    Fail-fast validations (always on):
        * Duplicate timestamps from the wire raise ``CanonicalBarsError``.
        * Non-monotonic timestamps raise ``CanonicalBarsError``.
        These are the canonical-input guards required by
        ``.claude/rules/numerical-rigor.md`` §"External-API ingestion".

    Opt-in completeness checks (``strict_completeness=True``, regular session only):
        * Every NYSE session in ``[start_date, end_date]`` must appear in
          the grouped output; missing sessions raise
          ``polygon_window_incomplete``.
        * Every session must include its scheduled open bar and the
          close-1 boundary bar (half-day aware via
          ``trading_calendar.session_close_minute_et``); missing
          boundary bars raise ``polygon_session_incomplete``.

    The completeness checks are off by default because Polygon minute
    aggregates legitimately omit minutes with no trades for thin /
    illiquid tickers — enforcing the boundary check on arbitrary
    Polygon runs rejects valid data (caught in Codex review of the
    original PR A hardening). The parity-test / freshness-canary
    paths opt in by passing ``strict_completeness=True``; the
    production sidecar orchestrator stays on the default.
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
    # routes through ``fetch_bars_chunks_raw`` precisely so the shared
    # validator — not a silent re-sort in ``fetch_bars_chunked`` — sees
    # the raw wire bars.
    assert_canonical_bar_stream(raw, symbol)

    # Session filter.
    if session == "regular":
        filtered = [b for b in raw if _is_rth(int(b["timestamp"]))]
    else:
        filtered = list(raw)

    # Convert + group by ET trading date.
    trade_bars = [polygon_bar_to_trade_bar(symbol, bar) for bar in filtered]
    grouped = group_by_trading_date(trade_bars)

    # Opt-in completeness checks. Per the Codex review on the original
    # PR A hardening attempt (closed #301), boundary-bar enforcement
    # would reject valid Polygon data for thin tickers; gate it behind
    # ``strict_completeness=True`` so only parity-grade callers opt in.
    if strict_completeness and session == "regular":
        _assert_window_complete(grouped=grouped, start_date=start_date, end_date=end_date, symbol=symbol)

    return [(d, grouped[d]) for d in sorted(grouped.keys())]


def _assert_window_complete(
    *,
    grouped: dict[date, list[TradeBar]],
    start_date: date,
    end_date: date,
    symbol: str,
) -> None:
    """Strict completeness check (opt-in via ``strict_completeness=True``).

    Two checks:
      1. Every NYSE session in ``[start_date, end_date]`` is present in
         ``grouped``; otherwise raise ``polygon_window_incomplete``.
      2. Every session has its scheduled open bar plus the close-1 boundary
         bar (half-day calendar aware); otherwise raise
         ``polygon_session_incomplete``.

    See ``.claude/rules/numerical-rigor.md`` §"External-API ingestion".
    Production paths default to lenient; the parity test and freshness
    canary opt in.
    """
    from app.lean_sidecar.trading_calendar import expected_sessions, session_close_minute_et, session_window_for_date

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
        bar_minutes: set[int] = set()
        for tb in bars:
            et_dt = tb.time.astimezone(_ET)
            bar_minutes.add(et_dt.hour * 60 + et_dt.minute)

        window = session_window_for_date(d)
        open_et = datetime.fromtimestamp(window.open_ms_utc / 1000, tz=_UTC).astimezone(_ET)
        open_minute = open_et.hour * 60 + open_et.minute
        close_minute = session_close_minute_et(d)
        required_last_bar_minute = close_minute - 1
        missing_boundaries: list[str] = []
        if open_minute not in bar_minutes:
            hh, mm = divmod(open_minute, 60)
            missing_boundaries.append(f"{hh:02d}:{mm:02d}")
        if required_last_bar_minute not in bar_minutes:
            hh, mm = divmod(required_last_bar_minute, 60)
            missing_boundaries.append(f"{hh:02d}:{mm:02d}")
        if missing_boundaries:
            raise CanonicalBarsError(
                f"polygon_session_incomplete: session {d.isoformat()} missing boundary "
                f"bar(s) {missing_boundaries} for {symbol}"
            )
