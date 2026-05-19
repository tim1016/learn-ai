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
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

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
