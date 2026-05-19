"""Canonical Polygon minute-bar source for the LEAN sidecar.

The provider Protocol lets the production orchestrator and tests share
the same fetch contract while sourcing bars from different places.
Tests inject ``RecordedPolygonFixtureProvider``; production injects
``PolygonProvider``. ``fetch_canonical_minute_bars`` (Task 3) applies
the RTH/extended filter and fail-fast monotonicity + dedup checks.

See docs/superpowers/specs/2026-05-19-lean-engine-polygon-parity-design.md.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from app.engine.data.polygon_export import _polygon_bar_to_trade_bar
from app.engine.data.trade_bar import TradeBar
from app.services.dataset_service import fetch_bars_chunked
from app.services.polygon_client import PolygonClientService


class CanonicalBarsProvider(Protocol):
    """Source of raw 1-minute Polygon-style bars for a single symbol."""

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
    """

    fixture_dir: Path

    def fetch_minute_bars(
        self,
        *,
        symbol: str,
        start_date: date,
        end_date: date,
        adjusted: bool,
    ) -> list[dict[str, Any]]:
        meta = json.loads((self.fixture_dir / "metadata.json").read_text())
        if meta.get("schema_version") != 1:
            raise FixtureMetadataMismatchError(
                f"fixture {self.fixture_dir.name!r} has metadata schema_version="
                f"{meta.get('schema_version')!r}; this provider supports only schema_version=1"
            )
        expected = (
            ("symbol", meta["symbol"], symbol),
            ("from_date", meta["from_date"], start_date.isoformat()),
            ("to_date", meta["to_date"], end_date.isoformat()),
            ("adjusted", meta["adjusted"], adjusted),
        )
        mismatches = [
            (field, fixture_val, asked_val) for field, fixture_val, asked_val in expected if fixture_val != asked_val
        ]
        if mismatches:
            details = "; ".join(
                f"{field}: fixture={fixture_val!r} asked={asked_val!r}" for field, fixture_val, asked_val in mismatches
            )
            raise FixtureMetadataMismatchError(f"fixture {self.fixture_dir.name!r} does not match request: {details}")
        bars: list[dict[str, Any]] = json.loads((self.fixture_dir / "bars.json").read_text())
        return bars


@dataclass(frozen=True, slots=True)
class PolygonProvider:
    """Live Polygon fetch via the existing chunked aggregator.

    Always requests 1-minute bars at multiplier 1 — strategy timeframes
    are produced by per-engine consolidation, not by Polygon-native
    aggregates. See spec §"Polygon data source".
    """

    polygon: PolygonClientService

    def fetch_minute_bars(
        self,
        *,
        symbol: str,
        start_date: date,
        end_date: date,
        adjusted: bool,
    ) -> list[dict[str, Any]]:
        return fetch_bars_chunked(
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

    # Fail-fast validation: strict monotonic, no duplicates.
    prev_ts: int | None = None
    seen: set[int] = set()
    for bar in raw:
        ts = int(bar["timestamp"])
        if ts in seen:
            raise CanonicalBarsError(f"polygon_corrupt_timestamps: duplicate timestamp {ts} for {symbol}")
        if prev_ts is not None and ts <= prev_ts:
            raise CanonicalBarsError(
                f"polygon_corrupt_timestamps: non-monotonic timestamp {ts} after {prev_ts} for {symbol}"
            )
        seen.add(ts)
        prev_ts = ts

    # Session filter.
    if session == "regular":
        filtered = [b for b in raw if _is_rth(int(b["timestamp"]))]
    else:
        filtered = list(raw)

    # Convert + group by ET trading date.
    grouped: dict[date, list[TradeBar]] = defaultdict(list)
    for bar in filtered:
        tb = _polygon_bar_to_trade_bar(symbol, bar)
        et = tb.time.astimezone(_ET)
        grouped[et.date()].append(tb)

    return [(d, grouped[d]) for d in sorted(grouped.keys())]
