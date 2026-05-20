# LEAN ↔ Engine Parity on Polygon-Sourced Bars — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ground the LEAN-sidecar EMA-crossover validation plane by routing Polygon 1-minute RTH bars through both LEAN and the in-process engine via the same staged zips, and prove parity with one pinned-window integration test asserting per-bar indicator state and trade-by-trade equivalence.

**Architecture:** New `polygon_canonical` provider (Protocol seam — `PolygonProvider` for prod, `RecordedPolygonFixtureProvider` for tests). `TrustedRunRequest` gains `data_source: Literal["synthetic", "polygon"]`. Synthetic path unchanged; Polygon path stages real bars. LEAN EMA template reads symbol/bar_minutes/session/adjustment via `GetParameter`, removes wall-clock warmup, emits `observations.csv` + `state.csv`. Engine reads the same staged zips via `LeanMinuteDataReader`. Run manifest gains a `data_policy` sub-block (schema 2→3) with explicit input/strategy bars, session, adjustment, fixture identity.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, pytest, pytest-asyncio, Polygon SDK 1.12.5, LEAN container (existing sidecar), pandas not required for this branch.

**Spec:** `docs/superpowers/specs/2026-05-19-lean-engine-polygon-parity-design.md`

**Branch:** `feat/backfill-lean-runs` (current). No new worktree; continue committing here.

---

## File map

| Path | Action | Responsibility |
|---|---|---|
| `PythonDataService/app/lean_sidecar/polygon_canonical.py` | Create | `CanonicalBarsProvider` Protocol, `PolygonProvider`, `RecordedPolygonFixtureProvider`, `fetch_canonical_minute_bars`, `get_default_provider` factory |
| `PythonDataService/tests/lean_sidecar/test_polygon_canonical.py` | Create | Unit tests: RTH filter, monotonicity rejection, duplicate rejection, fixture provider metadata-mismatch detection |
| `PythonDataService/app/lean_sidecar/manifest.py` | Modify | Add `BarsSpec`, `DataPolicyManifest`; add `data_policy` field to `RunManifest`; bump `MANIFEST_SCHEMA_VERSION = 2 → 3` |
| `PythonDataService/tests/lean_sidecar/test_manifest.py` | Modify (if exists) or Create | Manifest serialization round-trip with `data_policy`; adjustment-vocabulary assertion |
| `PythonDataService/app/services/lean_sidecar_service.py` | Modify | `TrustedRunRequest` adds 4 fields; `run_trusted_sample` branches on `data_source`; `_build_manifest` populates `data_policy` and asserts `adjusted ⇔ Raw` |
| `PythonDataService/app/routers/lean_sidecar.py` | Modify | `TrustedRunRequestModel` adds 4 fields; `post_trusted_run` passes them through |
| `PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py` | Modify | Read new params; remove `SetWarmUp`; emit `observations.csv` + `state.csv`; reuse `_to_ms_utc` |
| `PythonDataService/tests/lean_sidecar/test_ema_crossover_template.py` | Modify | Extend AST tests: new param reads, no `SetWarmUp`, `state.csv` writer present |
| `PythonDataService/scripts/regenerate_polygon_fixture.py` | Create | Operator script: live Polygon fetch → `bars.json` + `metadata.json` + opens `attribution.md` |
| `PythonDataService/tests/fixtures/polygon_capture/<window-id>/bars.json` | Create (operator) | Captured Polygon minute bars |
| `PythonDataService/tests/fixtures/polygon_capture/<window-id>/metadata.json` | Create (operator) | Machine-readable fixture manifest |
| `PythonDataService/tests/fixtures/polygon_capture/<window-id>/attribution.md` | Create (operator) | Human narrative |
| `PythonDataService/tests/_helpers/parity.py` | Create | `assert_state_traces_match`, `assert_trade_equivalence` shared helpers |
| `PythonDataService/tests/integration/test_lean_engine_polygon_parity.py` | Create | The receipt test — runs LEAN sidecar, runs engine over staged zips, asserts state + trade parity |
| `PythonDataService/tests/slow/test_polygon_fixture_freshness.py` | Create | `@pytest.mark.slow` canary — re-fetches live Polygon, diffs against fixture |

---

## Task sequence

Tasks are ordered so each builds on prior tasks; later tasks reuse types and functions defined earlier. Subagent-driven execution should respect the order — Task 7 cannot start until Task 3 has committed the manifest types.

---

### Task 1: Provider Protocol + RecordedPolygonFixtureProvider (TDD)

**Files:**
- Create: `PythonDataService/app/lean_sidecar/polygon_canonical.py`
- Test: `PythonDataService/tests/lean_sidecar/test_polygon_canonical.py`

The fixture provider has no network dependency, so we can TDD it first. `PolygonProvider` (live) and `fetch_canonical_minute_bars` come in Tasks 2 and 3.

- [ ] **Step 1: Write failing test for RecordedPolygonFixtureProvider happy path**

Create `PythonDataService/tests/lean_sidecar/test_polygon_canonical.py`:

```python
"""Unit tests for app.lean_sidecar.polygon_canonical."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.lean_sidecar.polygon_canonical import RecordedPolygonFixtureProvider


def _write_fixture(
    tmp_path: Path,
    *,
    symbol: str = "SPY",
    from_date: str = "2025-01-06",
    to_date: str = "2025-01-10",
    bars: list[dict] | None = None,
) -> Path:
    """Write a minimal fixture directory and return its path."""
    fixture_dir = tmp_path / f"{symbol.lower()}_minute_{from_date}_{to_date}"
    fixture_dir.mkdir()
    bars = bars if bars is not None else [
        {"timestamp": 1736175600000, "open": 591.0, "high": 591.5, "low": 590.5,
         "close": 591.2, "volume": 1000},
    ]
    (fixture_dir / "bars.json").write_text(json.dumps(bars))
    (fixture_dir / "metadata.json").write_text(json.dumps({
        "schema_version": 1,
        "symbol": symbol,
        "from_date": from_date,
        "to_date": to_date,
        "timespan": "minute",
        "multiplier": 1,
        "adjusted": False,
        "session_prefilter": "none",
        "bar_count": len(bars),
        "fetched_at_ms_utc": 1737432000000,
        "polygon_sdk_version": "1.12.5",
        "bars_sha256": "0" * 64,
        "observed_trade_count": 1,
        "observed_first_entry_ms_utc": 1736178300000,
        "observed_first_exit_ms_utc": 1736182800000,
    }))
    return fixture_dir


def test_recorded_provider_returns_bars_when_metadata_matches(tmp_path: Path) -> None:
    fixture_dir = _write_fixture(tmp_path)
    provider = RecordedPolygonFixtureProvider(fixture_dir)

    bars = provider.fetch_minute_bars(
        symbol="SPY",
        start_date=date(2025, 1, 6),
        end_date=date(2025, 1, 10),
        adjusted=False,
    )

    assert len(bars) == 1
    assert bars[0]["close"] == 591.2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_polygon_canonical.py::test_recorded_provider_returns_bars_when_metadata_matches -v`

Expected: FAIL — `ImportError: cannot import name 'RecordedPolygonFixtureProvider'`.

- [ ] **Step 3: Create polygon_canonical.py with Protocol + RecordedPolygonFixtureProvider**

Create `PythonDataService/app/lean_sidecar/polygon_canonical.py`:

```python
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


@dataclass(frozen=True)
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
        expected = (
            ("symbol", meta["symbol"], symbol),
            ("from_date", meta["from_date"], start_date.isoformat()),
            ("to_date", meta["to_date"], end_date.isoformat()),
            ("adjusted", meta["adjusted"], adjusted),
        )
        mismatches = [(field, fixture_val, asked_val)
                      for field, fixture_val, asked_val in expected
                      if fixture_val != asked_val]
        if mismatches:
            details = "; ".join(
                f"{field}: fixture={fixture_val!r} asked={asked_val!r}"
                for field, fixture_val, asked_val in mismatches
            )
            raise FixtureMetadataMismatchError(
                f"fixture {self.fixture_dir.name!r} does not match request: {details}"
            )
        bars: list[dict[str, Any]] = json.loads((self.fixture_dir / "bars.json").read_text())
        return bars
```

- [ ] **Step 4: Run test to verify it passes**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_polygon_canonical.py::test_recorded_provider_returns_bars_when_metadata_matches -v`

Expected: PASS.

- [ ] **Step 5: Write tests for metadata-mismatch rejection**

Append to `test_polygon_canonical.py`:

```python
@pytest.mark.parametrize("field,bad_value,asked", [
    ("symbol", "QQQ", {"symbol": "SPY"}),
    ("from_date", "2025-02-01", {"from_date": "2025-01-06"}),
    ("to_date", "2025-02-05", {"to_date": "2025-01-10"}),
    ("adjusted", True, {"adjusted": False}),
])
def test_recorded_provider_rejects_metadata_mismatch(
    tmp_path: Path, field: str, bad_value, asked: dict
) -> None:
    """Test does not silently load wrong bars when request drifts from fixture shape."""
    from app.lean_sidecar.polygon_canonical import FixtureMetadataMismatchError

    fixture_dir = _write_fixture(tmp_path)
    # Mutate fixture metadata to introduce the mismatch.
    meta = json.loads((fixture_dir / "metadata.json").read_text())
    meta[field] = bad_value
    (fixture_dir / "metadata.json").write_text(json.dumps(meta))

    provider = RecordedPolygonFixtureProvider(fixture_dir)
    with pytest.raises(FixtureMetadataMismatchError, match=field):
        provider.fetch_minute_bars(
            symbol=asked.get("symbol", "SPY"),
            start_date=date.fromisoformat(asked.get("from_date", "2025-01-06")),
            end_date=date.fromisoformat(asked.get("to_date", "2025-01-10")),
            adjusted=asked.get("adjusted", False),
        )
```

- [ ] **Step 6: Run tests and verify all pass**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_polygon_canonical.py -v`

Expected: 5 PASS (1 happy path + 4 parameterized mismatches).

- [ ] **Step 7: Lint and commit**

Run: `ruff check PythonDataService/app/lean_sidecar/polygon_canonical.py PythonDataService/tests/lean_sidecar/test_polygon_canonical.py`

Expected: no warnings.

```bash
git add PythonDataService/app/lean_sidecar/polygon_canonical.py PythonDataService/tests/lean_sidecar/test_polygon_canonical.py
git commit -m "$(cat <<'EOF'
feat(lean-sidecar): canonical Polygon bars provider Protocol + fixture replay

CanonicalBarsProvider Protocol lets tests inject a fixture replay while
production fetches live. RecordedPolygonFixtureProvider asserts (symbol,
range, adjusted) against the fixture's metadata.json so a test cannot
silently load the wrong window.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: PolygonProvider (live) + get_default_provider factory

**Files:**
- Modify: `PythonDataService/app/lean_sidecar/polygon_canonical.py`
- Test: `PythonDataService/tests/lean_sidecar/test_polygon_canonical.py`

`PolygonProvider` wraps `fetch_bars_chunked` so the orchestrator depends on the Protocol, not on `PolygonClientService` directly. `get_default_provider` is the seam tests monkey-patch.

- [ ] **Step 1: Write failing test for PolygonProvider delegation**

Append to `test_polygon_canonical.py`:

```python
from unittest.mock import MagicMock


def test_polygon_provider_delegates_to_fetch_bars_chunked(monkeypatch) -> None:
    from app.lean_sidecar import polygon_canonical

    fake_polygon = MagicMock()
    fake_bars = [{"timestamp": 1736175600000, "open": 1.0, "high": 1.0,
                  "low": 1.0, "close": 1.0, "volume": 0}]
    called_with: dict[str, object] = {}

    def fake_fetch(polygon, ticker, from_date, to_date, timespan, multiplier, adjusted, **_):
        called_with.update(
            ticker=ticker, from_date=from_date, to_date=to_date,
            timespan=timespan, multiplier=multiplier, adjusted=adjusted,
        )
        return fake_bars

    monkeypatch.setattr(polygon_canonical, "fetch_bars_chunked", fake_fetch)

    provider = polygon_canonical.PolygonProvider(polygon=fake_polygon)
    out = provider.fetch_minute_bars(
        symbol="SPY",
        start_date=date(2025, 1, 6),
        end_date=date(2025, 1, 10),
        adjusted=False,
    )

    assert out is fake_bars
    assert called_with == {
        "ticker": "SPY", "from_date": "2025-01-06", "to_date": "2025-01-10",
        "timespan": "minute", "multiplier": 1, "adjusted": False,
    }


def test_get_default_provider_returns_polygon_provider() -> None:
    from app.lean_sidecar.polygon_canonical import (
        PolygonProvider, get_default_provider,
    )

    provider = get_default_provider()
    assert isinstance(provider, PolygonProvider)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_polygon_canonical.py::test_polygon_provider_delegates_to_fetch_bars_chunked tests/lean_sidecar/test_polygon_canonical.py::test_get_default_provider_returns_polygon_provider -v`

Expected: FAIL — `ImportError: cannot import name 'PolygonProvider'` / `'get_default_provider'`.

- [ ] **Step 3: Add PolygonProvider and get_default_provider**

Append to `polygon_canonical.py`:

```python
from app.services.dataset_service import fetch_bars_chunked
from app.services.polygon_client import PolygonClientService


@dataclass(frozen=True)
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_polygon_canonical.py -v`

Expected: 7 PASS (5 from Task 1 + 2 new).

- [ ] **Step 5: Lint and commit**

```bash
ruff check PythonDataService/app/lean_sidecar/polygon_canonical.py PythonDataService/tests/lean_sidecar/test_polygon_canonical.py
git add PythonDataService/app/lean_sidecar/polygon_canonical.py PythonDataService/tests/lean_sidecar/test_polygon_canonical.py
git commit -m "$(cat <<'EOF'
feat(lean-sidecar): PolygonProvider for live fetch + get_default_provider factory

PolygonProvider wraps fetch_bars_chunked at 1-minute multiplier 1 —
strategy timeframes come from per-engine consolidation, never from
Polygon-native aggregates. get_default_provider() is the seam tests
monkey-patch to inject the fixture replay.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: fetch_canonical_minute_bars (RTH filter, fail-fast checks)

**Files:**
- Modify: `PythonDataService/app/lean_sidecar/polygon_canonical.py`
- Test: `PythonDataService/tests/lean_sidecar/test_polygon_canonical.py`

This is the public entry point the orchestrator calls. It applies the RTH filter, validates monotonicity and uniqueness, and groups by ET trading date — producing the `list[(date, list[TradeBar])]` shape `stage_minute_bars` consumes.

- [ ] **Step 1: Write failing test for happy path (RTH filter)**

Append to `test_polygon_canonical.py`:

```python
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def _bar(et_dt: datetime, close: float = 100.0) -> dict:
    """Build a Polygon-style dict with a ms-UTC timestamp at start-of-bar."""
    ts_ms = int(et_dt.astimezone(ZoneInfo("UTC")).timestamp() * 1000)
    return {
        "timestamp": ts_ms,
        "open": close - 0.05, "high": close + 0.05,
        "low": close - 0.10, "close": close, "volume": 1000,
    }


class _StubProvider:
    def __init__(self, bars: list[dict]) -> None:
        self._bars = bars

    def fetch_minute_bars(self, *, symbol, start_date, end_date, adjusted) -> list[dict]:
        return self._bars


def test_fetch_canonical_minute_bars_filters_to_rth() -> None:
    from app.lean_sidecar.polygon_canonical import fetch_canonical_minute_bars

    # 09:25 (pre-market), 09:30 (first RTH), 15:59 (last RTH), 16:00 (post-market)
    day = datetime(2025, 1, 6, tzinfo=_ET)
    pre = day.replace(hour=9, minute=25)
    open_min = day.replace(hour=9, minute=30)
    last_min = day.replace(hour=15, minute=59)
    post = day.replace(hour=16, minute=0)
    bars = [_bar(pre, 590.0), _bar(open_min, 591.0),
            _bar(last_min, 592.0), _bar(post, 593.0)]

    out = fetch_canonical_minute_bars(
        symbol="SPY",
        start_date=date(2025, 1, 6),
        end_date=date(2025, 1, 6),
        session="regular",
        adjustment="raw",
        provider=_StubProvider(bars),
    )

    # One trading day with two RTH bars (09:30, 15:59).
    assert len(out) == 1
    trading_date, trade_bars = out[0]
    assert trading_date == date(2025, 1, 6)
    assert [float(b.close) for b in trade_bars] == [591.0, 592.0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_polygon_canonical.py::test_fetch_canonical_minute_bars_filters_to_rth -v`

Expected: FAIL — `ImportError: cannot import name 'fetch_canonical_minute_bars'`.

- [ ] **Step 3: Implement fetch_canonical_minute_bars**

Append to `polygon_canonical.py`:

```python
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from app.engine.data.polygon_export import _polygon_bar_to_trade_bar
from app.engine.data.trade_bar import TradeBar

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
            raise CanonicalBarsError(
                f"polygon_corrupt_timestamps: duplicate timestamp {ts} for {symbol}"
            )
        if prev_ts is not None and ts <= prev_ts:
            raise CanonicalBarsError(
                f"polygon_corrupt_timestamps: non-monotonic timestamp {ts} "
                f"after {prev_ts} for {symbol}"
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_polygon_canonical.py::test_fetch_canonical_minute_bars_filters_to_rth -v`

Expected: PASS.

- [ ] **Step 5: Add fail-fast tests for monotonicity and duplicates**

Append to `test_polygon_canonical.py`:

```python
def test_fetch_canonical_rejects_duplicate_timestamps() -> None:
    from app.lean_sidecar.polygon_canonical import (
        CanonicalBarsError, fetch_canonical_minute_bars,
    )

    day = datetime(2025, 1, 6, 10, 0, tzinfo=_ET)
    bars = [_bar(day, 591.0), _bar(day, 591.5)]  # same timestamp

    with pytest.raises(CanonicalBarsError, match="duplicate"):
        fetch_canonical_minute_bars(
            symbol="SPY", start_date=date(2025, 1, 6), end_date=date(2025, 1, 6),
            session="regular", adjustment="raw", provider=_StubProvider(bars),
        )


def test_fetch_canonical_rejects_non_monotonic_timestamps() -> None:
    from app.lean_sidecar.polygon_canonical import (
        CanonicalBarsError, fetch_canonical_minute_bars,
    )

    day = datetime(2025, 1, 6, 10, 0, tzinfo=_ET)
    bars = [_bar(day, 591.0), _bar(day - timedelta(minutes=1), 590.5)]  # out of order

    with pytest.raises(CanonicalBarsError, match="non-monotonic"):
        fetch_canonical_minute_bars(
            symbol="SPY", start_date=date(2025, 1, 6), end_date=date(2025, 1, 6),
            session="regular", adjustment="raw", provider=_StubProvider(bars),
        )


def test_fetch_canonical_keeps_all_when_extended() -> None:
    from app.lean_sidecar.polygon_canonical import fetch_canonical_minute_bars

    day = datetime(2025, 1, 6, tzinfo=_ET)
    pre = day.replace(hour=9, minute=25)
    open_min = day.replace(hour=9, minute=30)
    post = day.replace(hour=16, minute=0)
    bars = [_bar(pre, 590.0), _bar(open_min, 591.0), _bar(post, 593.0)]

    out = fetch_canonical_minute_bars(
        symbol="SPY", start_date=date(2025, 1, 6), end_date=date(2025, 1, 6),
        session="extended", adjustment="raw", provider=_StubProvider(bars),
    )

    trading_date, trade_bars = out[0]
    assert len(trade_bars) == 3
```

- [ ] **Step 6: Run all tests and verify**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_polygon_canonical.py -v`

Expected: 10 PASS.

- [ ] **Step 7: Lint and commit**

```bash
ruff check PythonDataService/app/lean_sidecar/polygon_canonical.py PythonDataService/tests/lean_sidecar/test_polygon_canonical.py
git add PythonDataService/app/lean_sidecar/polygon_canonical.py PythonDataService/tests/lean_sidecar/test_polygon_canonical.py
git commit -m "$(cat <<'EOF'
feat(lean-sidecar): fetch_canonical_minute_bars with RTH filter + fail-fast checks

Public entry point the orchestrator calls. Applies the RTH session
filter (regular = [09:30, 16:00) ET), rejects duplicate or
non-monotonic Polygon timestamps per the external-API ingestion rule,
and groups by ET trading date into the shape stage_minute_bars
consumes.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: BarsSpec + DataPolicyManifest dataclasses + schema bump

**Files:**
- Modify: `PythonDataService/app/lean_sidecar/manifest.py`
- Test: `PythonDataService/tests/lean_sidecar/test_manifest.py` (read first to see existing patterns; modify or create)

The manifest gains `data_policy` as a mandatory field, which forces the schema bump `2 → 3`. The adjustment-vocabulary assertion lives in `_build_manifest` (Task 8); here we just add the types.

- [ ] **Step 1: Read existing manifest module and test**

Run: read `PythonDataService/app/lean_sidecar/manifest.py` end-to-end, and check whether `PythonDataService/tests/lean_sidecar/test_manifest.py` exists. Note the existing dataclass patterns (slots, frozen, serialization helpers like `to_dict` / `write_manifest`).

- [ ] **Step 2: Write failing test for DataPolicyManifest serialization**

Add to (or create) `PythonDataService/tests/lean_sidecar/test_manifest.py`:

```python
def test_data_policy_manifest_round_trips_synthetic_shape() -> None:
    from app.lean_sidecar.manifest import BarsSpec, DataPolicyManifest

    dp = DataPolicyManifest(
        source="synthetic",
        symbol="SPY",
        adjusted=False,
        session="regular",
        input_bars=BarsSpec(timespan="minute", multiplier=1),
        strategy_bars=BarsSpec(timespan="minute", multiplier=15),
        timestamp_policy="bar_close_ms_utc",
        timezone="America/New_York",
        fixture_id=None,
        fixture_sha256=None,
    )

    assert dp.source == "synthetic"
    assert dp.input_bars.multiplier == 1
    assert dp.strategy_bars.multiplier == 15
    assert dp.fixture_id is None


def test_manifest_schema_version_is_3() -> None:
    from app.lean_sidecar.manifest import MANIFEST_SCHEMA_VERSION
    assert MANIFEST_SCHEMA_VERSION == 3
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_manifest.py::test_data_policy_manifest_round_trips_synthetic_shape tests/lean_sidecar/test_manifest.py::test_manifest_schema_version_is_3 -v`

Expected: FAIL — import error and schema version is 2.

- [ ] **Step 4: Add BarsSpec, DataPolicyManifest; bump schema version**

In `PythonDataService/app/lean_sidecar/manifest.py`:

a. Change `MANIFEST_SCHEMA_VERSION = 2` to `MANIFEST_SCHEMA_VERSION = 3`.

b. Add the new dataclasses near the existing `WindowMs` / `StagedDataManifest` definitions:

```python
@dataclass(frozen=True, slots=True)
class BarsSpec:
    """Polygon-style (timespan, multiplier) pair.

    ``timespan`` matches Polygon's API vocabulary so a reviewer can map
    manifest values directly to /v2/aggs query parameters.
    """

    timespan: Literal["minute", "hour", "day"]
    multiplier: int


@dataclass(frozen=True, slots=True)
class DataPolicyManifest:
    """Where the bars came from and what processing they got.

    Separate from the existing top-level ``fill_forward`` /
    ``data_adjustment_policy`` / ``data_normalization_mode`` fields,
    which encode execution-time policy. This block encodes data
    provenance: "what bars did we feed in, and how were they
    constructed?"

    ``fixture_id`` and ``fixture_sha256`` are populated only when the
    run was driven by a ``RecordedPolygonFixtureProvider`` (i.e., a
    parity-test run). Live Polygon runs leave both ``None``.
    """

    source: Literal["synthetic", "polygon"]
    symbol: str
    adjusted: bool
    session: Literal["regular", "extended"]
    input_bars: BarsSpec
    strategy_bars: BarsSpec
    timestamp_policy: Literal["bar_close_ms_utc"]
    timezone: Literal["America/New_York"]
    fixture_id: str | None
    fixture_sha256: str | None
```

c. Add `data_policy: DataPolicyManifest` as a required field on `RunManifest` (placed near `staged_data` for readability).

d. If the existing `write_manifest` / `to_dict` serializer uses `dataclasses.asdict`, no changes needed — nested dataclasses serialize through. Verify by adding a JSON round-trip test if the existing tests don't already cover this.

- [ ] **Step 5: Run tests to verify they pass**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_manifest.py -v`

Expected: 2 new PASS plus all existing manifest tests pass.

- [ ] **Step 6: Run full lean_sidecar test suite to catch consumers**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/ -v`

Expected: failures in tests that construct `RunManifest` without `data_policy`. These need to be fixed in the next step.

- [ ] **Step 7: Fix existing RunManifest constructions**

For each test that constructs `RunManifest` directly (search: `grep -rn "RunManifest(" PythonDataService/tests/`), add a default-shape `DataPolicyManifest` argument. Use this builder helper at the top of each affected test module:

```python
def _default_data_policy() -> DataPolicyManifest:
    return DataPolicyManifest(
        source="synthetic",
        symbol="SPY",
        adjusted=False,
        session="regular",
        input_bars=BarsSpec(timespan="minute", multiplier=1),
        strategy_bars=BarsSpec(timespan="minute", multiplier=15),
        timestamp_policy="bar_close_ms_utc",
        timezone="America/New_York",
        fixture_id=None,
        fixture_sha256=None,
    )
```

- [ ] **Step 8: Re-run lean_sidecar tests and verify all pass**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/ -v`

Expected: all PASS.

- [ ] **Step 9: Lint and commit**

```bash
ruff check PythonDataService/app/lean_sidecar/manifest.py PythonDataService/tests/lean_sidecar/
git add PythonDataService/app/lean_sidecar/manifest.py PythonDataService/tests/lean_sidecar/
git commit -m "$(cat <<'EOF'
feat(lean-sidecar): add data_policy manifest sub-block; bump schema 2→3

New BarsSpec and DataPolicyManifest dataclasses capture data provenance
(source, symbol, adjustment, session, input vs strategy bars, fixture
identity) separately from execution-policy fields. data_policy is a
mandatory RunManifest field, forcing the schema bump.

Existing direct RunManifest constructions in tests get a default-shape
DataPolicyManifest helper so synthetic-path coverage is unchanged.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: TrustedRunRequest dataclass fields

**Files:**
- Modify: `PythonDataService/app/services/lean_sidecar_service.py` (the `TrustedRunRequest` dataclass)
- Test: `PythonDataService/tests/lean_sidecar/test_lean_sidecar_service.py` (read first; modify or create)

- [ ] **Step 1: Read the existing TrustedRunRequest at lean_sidecar_service.py:137-205**

Note the existing fields, the `frozen=True, slots=True` style, and the `start_date` / `end_date` derived properties. New fields go between `template` and any property.

- [ ] **Step 2: Write failing tests for new fields and defaults**

Add to `tests/lean_sidecar/test_lean_sidecar_service.py` (create if missing):

```python
def test_trusted_run_request_defaults_to_synthetic_15min_regular_raw() -> None:
    from app.services.lean_sidecar_service import TrustedRunRequest

    req = TrustedRunRequest(
        run_id="test-defaults",
        symbol="SPY",
        start_ms_utc=1736175600000,
        end_ms_utc=1736607600000,
        starting_cash=100_000.0,
    )

    assert req.data_source == "synthetic"
    assert req.bar_minutes == 15
    assert req.session == "regular"
    assert req.adjustment == "raw"


def test_trusted_run_request_accepts_polygon_data_source() -> None:
    from app.services.lean_sidecar_service import TrustedRunRequest

    req = TrustedRunRequest(
        run_id="test-polygon",
        symbol="SPY",
        start_ms_utc=1736175600000,
        end_ms_utc=1736607600000,
        starting_cash=100_000.0,
        data_source="polygon",
        bar_minutes=15,
        session="regular",
        adjustment="raw",
    )

    assert req.data_source == "polygon"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_lean_sidecar_service.py -v -k "trusted_run_request"`

Expected: FAIL — `TypeError: ... unexpected keyword argument 'data_source'`.

- [ ] **Step 4: Add the four new fields to TrustedRunRequest**

In `PythonDataService/app/services/lean_sidecar_service.py`, modify the `TrustedRunRequest` dataclass — add after the `template` field, before the `@property` definitions:

```python
    # Phase 6a — data-provenance plumbing for the parity contract.
    # See docs/superpowers/specs/2026-05-19-lean-engine-polygon-parity-design.md.
    data_source: Literal["synthetic", "polygon"] = "synthetic"
    # Pinned to 15 in this branch — the engine algorithm is 15-min only and
    # EXIT_BARS=5 is tied to that period. Widening this Literal is a
    # deliberate future change.
    bar_minutes: Literal[15] = 15
    session: Literal["regular", "extended"] = "regular"
    adjustment: Literal["raw"] = "raw"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_lean_sidecar_service.py -v -k "trusted_run_request"`

Expected: 2 PASS.

- [ ] **Step 6: Lint and commit**

```bash
ruff check PythonDataService/app/services/lean_sidecar_service.py PythonDataService/tests/lean_sidecar/test_lean_sidecar_service.py
git add PythonDataService/app/services/lean_sidecar_service.py PythonDataService/tests/lean_sidecar/test_lean_sidecar_service.py
git commit -m "$(cat <<'EOF'
feat(lean-sidecar): TrustedRunRequest fields for data_source, bar_minutes, session, adjustment

bar_minutes is Literal[15] in this branch — tunable strategy params
remain a non-goal; the field exists to be explicit in the request and
manifest, not to be varied.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: TrustedRunRequestModel Pydantic fields + router wiring

**Files:**
- Modify: `PythonDataService/app/routers/lean_sidecar.py` (TrustedRunRequestModel at line 161; post_trusted_run at line 420)
- Test: `PythonDataService/tests/routers/test_lean_sidecar_router.py` (read first; modify or create)

Without this task the dataclass fields exist but cannot be reached through the API.

- [ ] **Step 1: Read the router and find both touch points**

Read `PythonDataService/app/routers/lean_sidecar.py` lines 155-280 (Pydantic model + validator) and lines 415-450 (post_trusted_run handler). Note the existing `model_config = ConfigDict(extra="forbid")` and `Literal` usage on `template`.

- [ ] **Step 2: Write failing test for new Pydantic fields**

Add to `tests/routers/test_lean_sidecar_router.py`:

```python
def test_trusted_run_request_model_accepts_new_fields() -> None:
    from app.routers.lean_sidecar import TrustedRunRequestModel

    payload = {
        "run_id": "test-payload",
        "symbol": "SPY",
        "start_ms_utc": 1736175600000,
        "end_ms_utc": 1736607600000,
        "starting_cash": 100_000.0,
        "data_source": "polygon",
        "bar_minutes": 15,
        "session": "regular",
        "adjustment": "raw",
    }

    model = TrustedRunRequestModel(**payload)
    assert model.data_source == "polygon"
    assert model.bar_minutes == 15


def test_trusted_run_request_model_rejects_bar_minutes_other_than_15() -> None:
    from pydantic import ValidationError
    from app.routers.lean_sidecar import TrustedRunRequestModel

    with pytest.raises(ValidationError):
        TrustedRunRequestModel(
            run_id="test-bad-bm", symbol="SPY",
            start_ms_utc=1736175600000, end_ms_utc=1736607600000,
            starting_cash=100_000.0, bar_minutes=30,
        )


def test_trusted_run_request_model_defaults_match_dataclass() -> None:
    from app.routers.lean_sidecar import TrustedRunRequestModel

    model = TrustedRunRequestModel(
        run_id="test-def", symbol="SPY",
        start_ms_utc=1736175600000, end_ms_utc=1736607600000,
        starting_cash=100_000.0,
    )

    assert model.data_source == "synthetic"
    assert model.bar_minutes == 15
    assert model.session == "regular"
    assert model.adjustment == "raw"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `podman exec polygon-data-service python -m pytest tests/routers/test_lean_sidecar_router.py -v -k "trusted_run_request_model"`

Expected: FAIL on the first two; possibly errors on extra-forbid for the third.

- [ ] **Step 4: Add the four fields to TrustedRunRequestModel**

In `app/routers/lean_sidecar.py`, after the `template` field on `TrustedRunRequestModel` (around line 238):

```python
    data_source: Literal["synthetic", "polygon"] = Field(
        default="synthetic",
        description=(
            "Phase 6a — whether to stage synthetic deci-cent-clean bars "
            "(default; back-compat for buy-and-hold and reconciliation "
            "templates) or fetch Polygon 1-minute bars via the canonical "
            "provider. Polygon path required for ema_crossover parity "
            "runs."
        ),
    )
    bar_minutes: Literal[15] = Field(
        default=15,
        description=(
            "Strategy-bar consolidation period in minutes. Pinned to 15 "
            "in this branch — the EMA template's EXIT_BARS=5 time-stop "
            "is tied to this period. Widening is a deliberate future "
            "change."
        ),
    )
    session: Literal["regular", "extended"] = Field(
        default="regular",
        description=(
            "Trading session filter. 'regular' = [09:30, 16:00) ET; "
            "'extended' keeps pre/post-market bars. Both engines see "
            "the same staged bars after this filter."
        ),
    )
    adjustment: Literal["raw"] = Field(
        default="raw",
        description=(
            "Price-adjustment policy. Phase 1 supports only 'raw' "
            "(no split/dividend adjustment); LEAN's DataNormalizationMode "
            "is pinned to Raw to match."
        ),
    )
```

- [ ] **Step 5: Wire the fields into the post_trusted_run constructor**

Find `request = TrustedRunRequest(` in `post_trusted_run` (around line 427) and add the four new arguments:

```python
    request = TrustedRunRequest(
        run_id=payload.run_id,
        symbol=payload.symbol,
        start_ms_utc=payload.start_ms_utc,
        end_ms_utc=payload.end_ms_utc,
        starting_cash=payload.starting_cash,
        algorithm_source=payload.algorithm_source,
        template=payload.template,
        data_source=payload.data_source,
        bar_minutes=payload.bar_minutes,
        session=payload.session,
        adjustment=payload.adjustment,
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `podman exec polygon-data-service python -m pytest tests/routers/test_lean_sidecar_router.py -v -k "trusted_run_request_model"`

Expected: 3 PASS.

- [ ] **Step 7: Lint and commit**

```bash
ruff check PythonDataService/app/routers/lean_sidecar.py PythonDataService/tests/routers/test_lean_sidecar_router.py
git add PythonDataService/app/routers/lean_sidecar.py PythonDataService/tests/routers/test_lean_sidecar_router.py
git commit -m "$(cat <<'EOF'
feat(lean-sidecar): expose data_source, bar_minutes, session, adjustment via API

TrustedRunRequestModel gains the four Pydantic fields with matching
Literal types and defaults. post_trusted_run passes them through to
the dataclass. Without this wiring the dataclass fields exist but
cannot be driven from the HTTP boundary.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: EMA template — read params, drop SetWarmUp, emit observations.csv + state.csv

**Files:**
- Modify: `PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py`
- Test: `PythonDataService/tests/lean_sidecar/test_ema_crossover_template.py`

The template is a Python source *string* — tests are AST-level. The template runs inside the LEAN container against `AlgorithmImports`, which the test environment doesn't have, so direct execution isn't possible here. Behavior is verified end-to-end by the parity test in Task 11.

- [ ] **Step 1: Write failing AST tests for the new contract**

Add to `tests/lean_sidecar/test_ema_crossover_template.py`:

```python
def test_template_reads_new_parameters() -> None:
    from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE

    src = EMA_CROSSOVER_SOURCE
    assert 'GetParameter("symbol")' in src
    assert 'GetParameter("bar_minutes")' in src
    assert 'GetParameter("session")' in src
    assert 'GetParameter("adjustment")' in src


def test_template_no_longer_sets_wall_clock_warmup() -> None:
    from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE
    assert "SetWarmUp" not in EMA_CROSSOVER_SOURCE, (
        "Wall-clock warmup must be removed; both engines gate on indicator readiness only"
    )


def test_template_writes_observations_csv_and_state_csv() -> None:
    from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE
    assert "observations.csv" in EMA_CROSSOVER_SOURCE
    assert "state.csv" in EMA_CROSSOVER_SOURCE


def test_template_state_csv_header_matches_spec() -> None:
    """state.csv must have exactly the columns the parity test asserts on."""
    from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE
    assert "ts_ms_utc,close,ema_fast,ema_slow,rsi,cross_state,signal" in EMA_CROSSOVER_SOURCE


def test_template_rejects_non_15_bar_minutes() -> None:
    from app.lean_sidecar.trusted_samples.ema_crossover import EMA_CROSSOVER_SOURCE
    # Defense-in-depth check at the strategy layer.
    assert "bar_minutes" in EMA_CROSSOVER_SOURCE
    assert "raise ValueError" in EMA_CROSSOVER_SOURCE
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_ema_crossover_template.py -v`

Expected: 5 new tests FAIL.

- [ ] **Step 3: Replace ema_crossover.py with the updated template**

Overwrite `PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py`:

```python
"""EMA(5)/EMA(10) crossover trusted template — LEAN parity oracle for spec strategy.

Mirrors PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json
exactly. Strategy parameters (period, gap, RSI band, time stop) are class
constants — not GetParameter values — so this template is a deterministic
oracle: any change to the parameters is a deliberate code change, not a
runtime config drift.

Runtime parameters (symbol, bar_minutes, session, adjustment) ARE read via
GetParameter because they describe the data contract, not the strategy logic.
The orchestrator passes them through LeanConfig.parameters; the parity test
asserts the values reach the algorithm correctly.

Fill model: LEAN's default ImmediateFillModel fills market orders at
bar.EndTime / bar.Close — matches Engine Lab's signal_bar_close mode.
See docs/references/fill-model-parity-spike-2026-05-19.md.

Bar consumption proof: observations.csv (every minute bar received).
Decision state proof: state.csv (one row per consolidated bar after warmup).
"""

from __future__ import annotations

EMA_CROSSOVER_SOURCE = '''\
from AlgorithmImports import *
from datetime import datetime
from zoneinfo import ZoneInfo


_ET = ZoneInfo("America/New_York")


def _to_ms_utc(dt):
    """Normalize a QC-supplied Python datetime to int64 ms UTC.

    QC's Python bridge passes bar.EndTime as a naive datetime in the
    algorithm timezone (ET for US equities). Attaching the ET zone
    before .timestamp() is the only safe way to convert to a UTC epoch.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_ET)
    return int(dt.timestamp() * 1000)


class MyAlgorithm(QCAlgorithm):
    """EMA(5)/EMA(10) crossover with RSI(14) gate on 15-min consolidated bars.

    Validation oracle for the Engine Lab spec at
    PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json.
    """

    FAST_PERIOD = 5
    SLOW_PERIOD = 10
    RSI_PERIOD = 14
    EXIT_BARS = 5
    GAP_MIN = 0.20
    RSI_LO = 50
    RSI_HI = 70

    def Initialize(self):
        start = self.GetParameter("start_date") or "2025-01-06"
        end = self.GetParameter("end_date") or "2025-01-10"
        cash = float(self.GetParameter("starting_cash") or "100000")
        symbol_str = self.GetParameter("symbol") or "SPY"
        bar_minutes_str = self.GetParameter("bar_minutes") or "15"
        session = self.GetParameter("session") or "regular"
        adjustment = self.GetParameter("adjustment") or "raw"

        bar_minutes = int(bar_minutes_str)
        if bar_minutes != 15:
            raise ValueError(
                "bar_minutes=" + str(bar_minutes) + " not supported; "
                "EXIT_BARS=5 is tied to a 15-min consolidator in this branch"
            )

        if adjustment != "raw":
            raise ValueError("adjustment=" + str(adjustment) + " not supported; only 'raw' in Phase 1")

        sy, sm, sd = (int(x) for x in start.split("-"))
        ey, em, ed = (int(x) for x in end.split("-"))
        self.SetStartDate(sy, sm, sd)
        self.SetEndDate(ey, em, ed)
        self.SetCash(cash)

        equity = self.AddEquity(
            symbol_str,
            Resolution.Minute,
            fillForward=False,
            extendedMarketHours=(session == "extended"),
        )
        equity.SetDataNormalizationMode(DataNormalizationMode.Raw)
        self.symbol = equity.Symbol

        self.consolidator = TradeBarConsolidator(timedelta(minutes=bar_minutes))
        self.consolidator.DataConsolidated += self.OnConsolidatedBar
        self.SubscriptionManager.AddConsolidator(self.symbol, self.consolidator)

        self.ema_fast = ExponentialMovingAverage(self.FAST_PERIOD)
        self.ema_slow = ExponentialMovingAverage(self.SLOW_PERIOD)
        self.rsi = RelativeStrengthIndex(self.RSI_PERIOD, MovingAverageType.Wilders)

        self.prev_fast = None
        self.prev_slow = None
        self.bars_held = 0
        self.in_trade = False

        # Indicator-readiness only — no wall-clock SetWarmUp. Both engines
        # use the same gate so state.csv row counts align.
        self.SetBenchmark(lambda dt: 100)

        obs_path = self.ObjectStore.GetFilePath("observations.csv")
        with open(obs_path, "w") as f:
            f.write("ms_utc,close\\n")
        self._obs_path = obs_path

        state_path = self.ObjectStore.GetFilePath("state.csv")
        with open(state_path, "w") as f:
            f.write("ts_ms_utc,close,ema_fast,ema_slow,rsi,cross_state,signal\\n")
        self._state_path = state_path

    def OnData(self, slice):
        bar = slice.Bars.get(self.symbol)
        if bar is None:
            return
        with open(self._obs_path, "a") as f:
            f.write(str(_to_ms_utc(bar.EndTime)) + "," + str(bar.Close) + "\\n")

    def OnConsolidatedBar(self, sender, bar):
        close = float(bar.Close)
        self.ema_fast.Update(bar.EndTime, close)
        self.ema_slow.Update(bar.EndTime, close)
        self.rsi.Update(bar.EndTime, close)

        if not (self.ema_fast.IsReady and self.ema_slow.IsReady and self.rsi.IsReady):
            self.prev_fast = float(self.ema_fast.Current.Value) if self.ema_fast.IsReady else None
            self.prev_slow = float(self.ema_slow.Current.Value) if self.ema_slow.IsReady else None
            return

        fast = float(self.ema_fast.Current.Value)
        slow = float(self.ema_slow.Current.Value)
        rsi = float(self.rsi.Current.Value)

        signal = "HOLD"
        if self.in_trade:
            self.bars_held += 1
            if self.bars_held >= self.EXIT_BARS:
                self.Liquidate(self.symbol)
                self.in_trade = False
                self.bars_held = 0
                signal = "EXIT"
        else:
            fresh_cross = (
                self.prev_fast is not None
                and self.prev_slow is not None
                and fast > slow
                and self.prev_fast <= self.prev_slow
            )
            gap_ok = (fast - slow) >= self.GAP_MIN
            rsi_ok = self.RSI_LO <= rsi <= self.RSI_HI
            if fresh_cross and gap_ok and rsi_ok:
                self.SetHoldings(self.symbol, 1.0)
                self.in_trade = True
                self.bars_held = 0
                signal = "ENTER"

        if fast > slow:
            cross_state = "above"
        elif fast < slow:
            cross_state = "below"
        else:
            cross_state = "equal"

        with open(self._state_path, "a") as f:
            f.write(
                str(_to_ms_utc(bar.EndTime)) + ","
                + str(close) + ","
                + str(fast) + ","
                + str(slow) + ","
                + str(rsi) + ","
                + cross_state + ","
                + signal + "\\n"
            )

        self.prev_fast, self.prev_slow = fast, slow

    def OnEndOfAlgorithm(self):
        if self.Portfolio[self.symbol].Invested:
            self.Liquidate(self.symbol)
'''
```

- [ ] **Step 4: Run all template tests and verify they pass**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_ema_crossover_template.py -v`

Expected: all tests PASS (5 new + existing).

- [ ] **Step 5: Lint and commit**

```bash
ruff check PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py PythonDataService/tests/lean_sidecar/test_ema_crossover_template.py
git add PythonDataService/app/lean_sidecar/trusted_samples/ema_crossover.py PythonDataService/tests/lean_sidecar/test_ema_crossover_template.py
git commit -m "$(cat <<'EOF'
feat(lean-sidecar): EMA template reads runtime params, drops SetWarmUp, emits state.csv

Runtime parameters (symbol, bar_minutes, session, adjustment) read via
GetParameter so the algorithm subscribes to exactly what was staged.
SetWarmUp removed — gating is indicator-readiness only, matching the
engine's _on_fifteen_minute_bar gate so state.csv row counts align.

observations.csv (every minute bar) mirrors buy-and-hold's bar-
consumption proof. state.csv (one row per consolidated bar after
warmup) is the decision-state receipt the parity test asserts on.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: run_trusted_sample Polygon branch + manifest population + adjustment assertion

**Files:**
- Modify: `PythonDataService/app/services/lean_sidecar_service.py` (`run_trusted_sample`, `_build_manifest`)
- Test: `PythonDataService/tests/lean_sidecar/test_lean_sidecar_service.py`

The router and dataclass now carry the new fields. This task wires them into the orchestration body and into the manifest.

- [ ] **Step 1: Write failing tests for the adjustment-vocabulary assertion**

`_build_manifest` takes many inputs (workspace, response, staged paths) so testing the `data_policy` shape through it requires either heavy mocking or an integration run. The integration coverage lives in Task 12 (which inspects the manifest after a real Polygon run). Here we add a *direct* unit test on the extracted assertion helper — small, focused, fast.

Add to `tests/lean_sidecar/test_lean_sidecar_service.py`:

```python
def test_build_manifest_raises_when_adjusted_disagrees_with_normalization_mode() -> None:
    """data_policy.adjusted=False MUST imply data_normalization_mode='Raw'."""
    from app.services.lean_sidecar_service import (
        LeanSidecarServiceError, _assert_adjustment_vocabulary_consistent,
    )

    # Helper extracted from _build_manifest for direct testability.
    with pytest.raises(LeanSidecarServiceError, match="adjustment_vocabulary_mismatch"):
        _assert_adjustment_vocabulary_consistent(adjusted=False, data_normalization_mode="Adjusted")
    with pytest.raises(LeanSidecarServiceError, match="adjustment_vocabulary_mismatch"):
        _assert_adjustment_vocabulary_consistent(adjusted=True, data_normalization_mode="Raw")

    # Happy paths return None.
    _assert_adjustment_vocabulary_consistent(adjusted=False, data_normalization_mode="Raw")
    _assert_adjustment_vocabulary_consistent(adjusted=True, data_normalization_mode="Adjusted")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_lean_sidecar_service.py -v -k "adjustment_vocabulary"`

Expected: FAIL — `ImportError: cannot import name '_assert_adjustment_vocabulary_consistent'`.

- [ ] **Step 3: Add the adjustment-vocabulary assertion helper**

Near the top of `app/services/lean_sidecar_service.py` (after the existing module helpers):

```python
def _assert_adjustment_vocabulary_consistent(
    *, adjusted: bool, data_normalization_mode: str,
) -> None:
    """Enforce data_policy.adjusted ⇔ data_normalization_mode at manifest build.

    The two fields encode the same intent in different vocabularies
    (Polygon's adjusted=False flag means 'raw' prices; LEAN's
    DataNormalizationMode 'Raw' means the same). A mismatch indicates
    an upstream wiring bug and must fail loud, not be silently
    reconciled.
    """
    if adjusted is False and data_normalization_mode != "Raw":
        raise LeanSidecarServiceError(
            f"adjustment_vocabulary_mismatch: adjusted=False requires "
            f"data_normalization_mode='Raw', got {data_normalization_mode!r}"
        )
    if adjusted is True and data_normalization_mode == "Raw":
        raise LeanSidecarServiceError(
            f"adjustment_vocabulary_mismatch: adjusted=True conflicts with "
            f"data_normalization_mode='Raw'"
        )
```

- [ ] **Step 4: Run the assertion tests and verify they pass**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_lean_sidecar_service.py -v -k "adjustment_vocabulary"`

Expected: PASS.

- [ ] **Step 5: Add the Polygon branch to run_trusted_sample**

In `run_trusted_sample`, replace the synthetic-only staging at line 445-446:

```python
    trading_dates = _iter_trading_dates(request.start_date, request.end_date)
    bars_by_date = [(d, _generate_synthetic_bars(request.symbol, d)) for d in trading_dates]
```

with a branch on `request.data_source`:

```python
    if request.data_source == "synthetic":
        trading_dates = _iter_trading_dates(request.start_date, request.end_date)
        bars_by_date = [(d, _generate_synthetic_bars(request.symbol, d)) for d in trading_dates]
    elif request.data_source == "polygon":
        from app.lean_sidecar.polygon_canonical import (
            fetch_canonical_minute_bars, get_default_provider,
        )

        provider = get_default_provider()
        bars_by_date = fetch_canonical_minute_bars(
            symbol=request.symbol,
            start_date=request.start_date,
            end_date=request.end_date,
            session=request.session,
            adjustment=request.adjustment,
            provider=provider,
        )
        if not bars_by_date:
            raise LeanSidecarServiceError(
                f"polygon_returned_zero_bars: window={request.start_date.isoformat()}.."
                f"{request.end_date.isoformat()}; symbol={request.symbol}"
            )
        trading_dates = [d for d, _ in bars_by_date]
    else:
        # Defense-in-depth — Pydantic Literal already rejects unknown values.
        raise LeanSidecarServiceError(f"unknown data_source: {request.data_source!r}")
```

- [ ] **Step 6: Pass the new params through LeanConfig**

In `run_trusted_sample`, modify the `LeanConfig(parameters={...})` block (around line 466):

```python
    config = LeanConfig(
        parameters={
            "start_date": request.start_date.isoformat(),
            "end_date": request.end_date.isoformat(),
            "starting_cash": str(request.starting_cash),
            "symbol": request.symbol,
            "bar_minutes": str(request.bar_minutes),
            "session": request.session,
            "adjustment": request.adjustment,
        }
    )
```

- [ ] **Step 7: Populate data_policy in _build_manifest**

In `_build_manifest`, build the `DataPolicyManifest` from the request:

```python
    from app.lean_sidecar.manifest import BarsSpec, DataPolicyManifest

    data_policy = DataPolicyManifest(
        source=request.data_source,
        symbol=request.symbol,
        adjusted=(request.adjustment != "raw"),  # raw ⇒ adjusted=False
        session=request.session,
        input_bars=BarsSpec(timespan="minute", multiplier=1),
        strategy_bars=BarsSpec(timespan="minute", multiplier=request.bar_minutes),
        timestamp_policy="bar_close_ms_utc",
        timezone="America/New_York",
        # Fixture identity is populated by the parity test through a
        # separate hook (Task 11) — production live-Polygon runs leave
        # both fields None.
        fixture_id=None,
        fixture_sha256=None,
    )

    _assert_adjustment_vocabulary_consistent(
        adjusted=data_policy.adjusted,
        data_normalization_mode="Raw",  # template pins Raw; widening is future work
    )
```

Pass `data_policy=data_policy` to the `RunManifest(...)` constructor.

- [ ] **Step 8: Run focused tests for the orchestrator and manifest**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/test_lean_sidecar_service.py tests/lean_sidecar/test_manifest.py -v`

Expected: all PASS.

- [ ] **Step 9: Run the existing synthetic-path tests as a regression check**

Run: `podman exec polygon-data-service python -m pytest tests/lean_sidecar/ -v -k "synthetic or trusted or buy_and_hold"`

Expected: all PASS — the synthetic branch's behavior must be byte-identical.

- [ ] **Step 10: Lint and commit**

```bash
ruff check PythonDataService/app/services/lean_sidecar_service.py PythonDataService/tests/lean_sidecar/test_lean_sidecar_service.py
git add PythonDataService/app/services/lean_sidecar_service.py PythonDataService/tests/lean_sidecar/test_lean_sidecar_service.py
git commit -m "$(cat <<'EOF'
feat(lean-sidecar): orchestrator Polygon branch + data_policy manifest population + raw≡Raw assert

run_trusted_sample branches on data_source: synthetic path unchanged
(byte-identical), polygon path fetches via the canonical provider,
fails fast on zero-bar windows. LeanConfig now passes symbol,
bar_minutes, session, adjustment so the template's GetParameter calls
resolve. _build_manifest populates DataPolicyManifest from the
request and asserts adjusted=False ⇔ data_normalization_mode="Raw"
to surface upstream wiring bugs at manifest construction time.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Fixture-capture script

**Files:**
- Create: `PythonDataService/scripts/regenerate_polygon_fixture.py`

The script lives outside the test suite; it's an operator tool the user runs once to produce the fixture for the parity test (Task 10 is the operator-run step).

- [ ] **Step 1: Create the script**

Create `PythonDataService/scripts/regenerate_polygon_fixture.py`:

```python
"""Capture a Polygon minute-bar fixture for the LEAN-vs-engine parity test.

Usage:
    python scripts/regenerate_polygon_fixture.py SPY 2025-01-06 2025-01-10

Outputs:
    tests/fixtures/polygon_capture/<symbol>_minute_<from>_<to>/
        bars.json         — raw Polygon bar dicts (timestamp, ohlcv)
        metadata.json     — machine-readable manifest
        attribution.md    — opened in the operator's editor for narrative

Requires:
    POLYGON_API_KEY environment variable set.

After running:
    1. Run the LEAN EMA template against this fixture (operator step:
       `python scripts/probe_lean_ema_trade_count.py <fixture-dir>` —
       to be added when the fixture script is first exercised).
    2. If observed_trade_count == 0, pick a different window. Zero-trade
       fixtures cannot serve as parity receipts.
    3. Edit attribution.md with the rationale for the window.
    4. Commit bars.json + metadata.json + attribution.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from app.services.polygon_client import PolygonClientService
from app.services.dataset_service import fetch_bars_chunked

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "polygon_capture"


def main(symbol: str, from_date: str, to_date: str) -> int:
    if not os.environ.get("POLYGON_API_KEY"):
        print("ERROR: POLYGON_API_KEY env var is required", file=sys.stderr)
        return 2

    polygon = PolygonClientService()
    print(f"Fetching {symbol} 1-minute bars from {from_date} to {to_date}...")
    bars = fetch_bars_chunked(
        polygon=polygon,
        ticker=symbol,
        from_date=from_date,
        to_date=to_date,
        timespan="minute",
        multiplier=1,
        adjusted=False,
    )
    print(f"Received {len(bars)} bars.")

    if not bars:
        print("ERROR: zero bars returned; pick a different window", file=sys.stderr)
        return 3

    fixture_dir = FIXTURE_ROOT / f"{symbol.lower()}_minute_{from_date}_{to_date}"
    fixture_dir.mkdir(parents=True, exist_ok=True)

    bars_json = json.dumps(bars, separators=(",", ":"))
    bars_path = fixture_dir / "bars.json"
    bars_path.write_text(bars_json)
    bars_sha256 = hashlib.sha256(bars_json.encode("utf-8")).hexdigest()

    # observed_trade_count is initially None; the operator updates it
    # after running the EMA template once.
    metadata = {
        "schema_version": 1,
        "symbol": symbol,
        "from_date": from_date,
        "to_date": to_date,
        "timespan": "minute",
        "multiplier": 1,
        "adjusted": False,
        "session_prefilter": "none",
        "bar_count": len(bars),
        "fetched_at_ms_utc": int(datetime.now(timezone.utc).timestamp() * 1000),
        "polygon_sdk_version": _polygon_sdk_version(),
        "bars_sha256": bars_sha256,
        "observed_trade_count": None,
        "observed_first_entry_ms_utc": None,
        "observed_first_exit_ms_utc": None,
    }
    (fixture_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))

    attribution = fixture_dir / "attribution.md"
    if not attribution.exists():
        attribution.write_text(
            f"# Polygon fixture: {symbol} {from_date}..{to_date}\n\n"
            f"**Captured:** {datetime.now(timezone.utc).isoformat()}\n\n"
            "## Why this window\n\n"
            "TODO: explain why this window was chosen (e.g., known to produce ≥1 EMA-crossover trade).\n\n"
            "## Observed trade count\n\n"
            "TODO: run the EMA template against this fixture and record the count in metadata.json.\n"
        )

    print(f"Wrote {bars_path} ({len(bars)} bars)")
    print(f"Wrote {fixture_dir / 'metadata.json'} (sha256={bars_sha256[:12]}...)")
    print(f"Edit {attribution} with narrative context, then update metadata.json")
    print("  observed_trade_count, observed_first_entry_ms_utc, observed_first_exit_ms_utc")
    return 0


def _polygon_sdk_version() -> str:
    try:
        from importlib.metadata import version
        return version("polygon-api-client")
    except Exception:
        return "unknown"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("symbol")
    parser.add_argument("from_date")
    parser.add_argument("to_date")
    args = parser.parse_args()
    sys.exit(main(args.symbol, args.from_date, args.to_date))
```

- [ ] **Step 2: Lint and commit**

```bash
ruff check PythonDataService/scripts/regenerate_polygon_fixture.py
git add PythonDataService/scripts/regenerate_polygon_fixture.py
git commit -m "$(cat <<'EOF'
feat(scripts): fixture-capture script for the LEAN-engine parity test

Operator tool: fetches Polygon 1-minute bars for a given window, writes
bars.json + metadata.json + a stub attribution.md. The operator updates
observed_trade_count in metadata.json after running the EMA template
once against the fixture; a zero-trade fixture must not be committed
(it cannot serve as a parity receipt).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: Operator captures the fixture (human-in-the-loop)

This task is a manual step the user performs. Document it explicitly so the agentic implementer knows when to pause and hand off.

- [ ] **Step 1: Operator sets POLYGON_API_KEY in the local environment**

Confirm `echo $POLYGON_API_KEY` is non-empty.

- [ ] **Step 2: Operator runs the fixture capture**

```bash
cd PythonDataService
python scripts/regenerate_polygon_fixture.py SPY 2025-01-06 2025-01-10
```

- [ ] **Step 3: Operator runs the LEAN EMA template once against the fixture to discover trade count**

For this branch, the simplest path is to run the existing `post_trusted_run` endpoint pointed at the fixture (with `get_default_provider` monkey-patched). The operator-run mechanism: a one-off `scripts/probe_ema_against_fixture.py` (to be created in a follow-up if this becomes routine) OR simply run the parity test once with `observed_trade_count=None` allowed, observe LEAN's emitted trade count, and update `metadata.json`.

Minimum acceptance: at least one fully-closed round-trip trade observed.

- [ ] **Step 4: If zero trades, retry with a different window**

Advance the start_date by one week and rerun Steps 2-3.

- [ ] **Step 5: Update metadata.json**

Edit `tests/fixtures/polygon_capture/<window-id>/metadata.json` to fill in `observed_trade_count`, `observed_first_entry_ms_utc`, `observed_first_exit_ms_utc`.

- [ ] **Step 6: Edit attribution.md with rationale**

Fill in the "Why this window" and "Observed trade count" sections in `attribution.md`.

- [ ] **Step 7: Commit the fixture**

```bash
git add PythonDataService/tests/fixtures/polygon_capture/<window-id>/
git commit -m "$(cat <<'EOF'
test(fixtures): pin Polygon SPY <from>..<to> fixture for LEAN-engine parity

Captured via scripts/regenerate_polygon_fixture.py. Observed
trade count: <N>. See attribution.md for rationale.

Co-Authored-By: <operator>
EOF
)"
```

---

### Task 11: Parity helpers — assert_state_traces_match, assert_trade_equivalence

**Files:**
- Create: `PythonDataService/tests/_helpers/parity.py`
- Create: `PythonDataService/tests/_helpers/__init__.py` (if not present)
- Test: `PythonDataService/tests/_helpers/test_parity_helpers.py`

These helpers are reused by the parity test (Task 12) and any future LEAN-vs-engine tests.

- [ ] **Step 1: Write failing tests for the helpers**

Create `PythonDataService/tests/_helpers/test_parity_helpers.py`:

```python
"""Unit tests for parity assertion helpers."""

from __future__ import annotations

from decimal import Decimal

import pytest


def test_assert_state_traces_match_passes_on_identical_rows() -> None:
    from tests._helpers.parity import assert_state_traces_match

    rows = [
        {"ts_ms_utc": 1736178300000, "close": 591.2, "ema_fast": 591.1,
         "ema_slow": 590.9, "rsi": 55.0, "cross_state": "above", "signal": "HOLD"},
    ]
    # Identical lists → no exception.
    assert_state_traces_match(rows, rows, atol=1e-9, rtol=0.0)


def test_assert_state_traces_match_passes_within_tolerance() -> None:
    from tests._helpers.parity import assert_state_traces_match

    a = [{"ts_ms_utc": 1, "close": 1.0, "ema_fast": 591.123456789,
          "ema_slow": 590.0, "rsi": 55.0, "cross_state": "above", "signal": "HOLD"}]
    b = [{"ts_ms_utc": 1, "close": 1.0, "ema_fast": 591.1234567895,
          "ema_slow": 590.0, "rsi": 55.0, "cross_state": "above", "signal": "HOLD"}]
    assert_state_traces_match(a, b, atol=1e-8, rtol=0.0)


def test_assert_state_traces_match_fails_on_row_count_mismatch() -> None:
    from tests._helpers.parity import assert_state_traces_match

    a = [{"ts_ms_utc": 1, "close": 1.0, "ema_fast": 1.0, "ema_slow": 1.0,
          "rsi": 50.0, "cross_state": "equal", "signal": "HOLD"}]
    with pytest.raises(AssertionError, match="row count"):
        assert_state_traces_match(a, [], atol=1e-9, rtol=0.0)


def test_assert_state_traces_match_fails_on_field_divergence() -> None:
    from tests._helpers.parity import assert_state_traces_match

    a = [{"ts_ms_utc": 1, "close": 1.0, "ema_fast": 1.0, "ema_slow": 1.0,
          "rsi": 50.0, "cross_state": "equal", "signal": "HOLD"}]
    b = [{"ts_ms_utc": 1, "close": 1.0, "ema_fast": 1.5, "ema_slow": 1.0,
          "rsi": 50.0, "cross_state": "equal", "signal": "HOLD"}]
    with pytest.raises(AssertionError, match="ema_fast"):
        assert_state_traces_match(a, b, atol=1e-9, rtol=0.0)


def test_assert_trade_equivalence_passes_on_identical_trades() -> None:
    from tests._helpers.parity import assert_trade_equivalence

    trades = [
        {"entry_ms_utc": 1736178300000, "exit_ms_utc": 1736182800000,
         "quantity": Decimal("168"), "entry_price": Decimal("591.20"),
         "exit_price": Decimal("592.50")},
    ]
    assert_trade_equivalence(trades, trades, fill_price_atol=Decimal("0.01"))


def test_assert_trade_equivalence_fails_on_price_drift_over_tolerance() -> None:
    from tests._helpers.parity import assert_trade_equivalence

    a = [{"entry_ms_utc": 1, "exit_ms_utc": 2, "quantity": Decimal("100"),
          "entry_price": Decimal("100.00"), "exit_price": Decimal("101.00")}]
    b = [{"entry_ms_utc": 1, "exit_ms_utc": 2, "quantity": Decimal("100"),
          "entry_price": Decimal("100.00"), "exit_price": Decimal("101.02")}]
    with pytest.raises(AssertionError, match="exit_price"):
        assert_trade_equivalence(a, b, fill_price_atol=Decimal("0.01"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `podman exec polygon-data-service python -m pytest tests/_helpers/test_parity_helpers.py -v`

Expected: FAIL — `ImportError: No module named 'tests._helpers.parity'`.

- [ ] **Step 3: Create the helper module**

Create `PythonDataService/tests/_helpers/__init__.py` if missing (empty file).

Create `PythonDataService/tests/_helpers/parity.py`:

```python
"""Shared parity-assertion helpers for LEAN-vs-engine receipts."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import math

_FLOAT_FIELDS = ("close", "ema_fast", "ema_slow", "rsi")
_EXACT_FIELDS = ("ts_ms_utc", "cross_state", "signal")


def assert_state_traces_match(
    lean_rows: list[dict[str, Any]],
    engine_rows: list[dict[str, Any]],
    *,
    atol: float,
    rtol: float,
) -> None:
    """Assert LEAN's state.csv ≡ engine's recorded snapshots row-by-row.

    First divergence raises AssertionError with both sides' full row
    and the field that broke tolerance.
    """
    if len(lean_rows) != len(engine_rows):
        raise AssertionError(
            f"state-trace row count mismatch: lean={len(lean_rows)}, "
            f"engine={len(engine_rows)}"
        )

    for i, (lr, er) in enumerate(zip(lean_rows, engine_rows, strict=True)):
        for field in _EXACT_FIELDS:
            if lr[field] != er[field]:
                raise AssertionError(
                    f"row {i}: exact-field {field!r} differs: "
                    f"lean={lr[field]!r} engine={er[field]!r}\n"
                    f"  lean row : {lr}\n  engine row: {er}"
                )
        for field in _FLOAT_FIELDS:
            lv, ev = float(lr[field]), float(er[field])
            if not math.isclose(lv, ev, abs_tol=atol, rel_tol=rtol):
                raise AssertionError(
                    f"row {i}: float-field {field!r} differs beyond "
                    f"atol={atol}, rtol={rtol}: lean={lv!r} engine={ev!r}\n"
                    f"  lean row : {lr}\n  engine row: {er}"
                )


def assert_trade_equivalence(
    lean_trades: list[dict[str, Any]],
    engine_trades: list[dict[str, Any]],
    *,
    fill_price_atol: Decimal,
) -> None:
    """Assert LEAN's trade list ≡ engine's trade list within fill-price tolerance.

    Exact match on timestamps and quantities; entry/exit prices within
    ``fill_price_atol`` (default $0.01 per the divergence taxonomy's
    FILL_PRICE_DRIFT category).
    """
    if len(lean_trades) != len(engine_trades):
        raise AssertionError(
            f"trade count mismatch: lean={len(lean_trades)}, engine={len(engine_trades)}"
        )

    for i, (lt, et) in enumerate(zip(lean_trades, engine_trades, strict=True)):
        for field in ("entry_ms_utc", "exit_ms_utc", "quantity"):
            if lt[field] != et[field]:
                raise AssertionError(
                    f"trade {i}: {field!r} differs: lean={lt[field]!r} engine={et[field]!r}\n"
                    f"  lean : {lt}\n  engine: {et}"
                )
        for field in ("entry_price", "exit_price"):
            diff = abs(Decimal(str(lt[field])) - Decimal(str(et[field])))
            if diff > fill_price_atol:
                raise AssertionError(
                    f"trade {i}: {field!r} differs beyond {fill_price_atol}: "
                    f"lean={lt[field]} engine={et[field]} diff={diff}\n"
                    f"  lean : {lt}\n  engine: {et}"
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `podman exec polygon-data-service python -m pytest tests/_helpers/test_parity_helpers.py -v`

Expected: 6 PASS.

- [ ] **Step 5: Lint and commit**

```bash
ruff check PythonDataService/tests/_helpers/
git add PythonDataService/tests/_helpers/
git commit -m "$(cat <<'EOF'
test(helpers): assert_state_traces_match and assert_trade_equivalence

Shared parity-assertion helpers for LEAN-vs-engine receipts. First
divergence raises with both sides' full row so failures are
self-diagnosing. Float fields gated by atol/rtol; ts_ms_utc and
cross_state/signal require exact match.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: The receipt — integration test

**Files:**
- Create: `PythonDataService/tests/integration/__init__.py` (if missing)
- Create: `PythonDataService/tests/integration/test_lean_engine_polygon_parity.py`

This is the test that proves the branch's claim. It requires the LEAN launcher running and the fixture from Task 10.

- [ ] **Step 1: Create the integration test**

Create `PythonDataService/tests/integration/test_lean_engine_polygon_parity.py`:

```python
"""LEAN ↔ engine parity on Polygon-sourced bars (the receipt test).

Runs the LEAN sidecar in Polygon-source mode against a recorded
fixture, then runs the in-process engine over the same staged LEAN
zips. Asserts per-bar indicator state equivalence (state.csv ≡
DecisionSnapshot stream) and trade-by-trade equivalence.

Skipped without ``LEAN_LAUNCHER_URL`` because LEAN must be reachable
to produce state.csv. The fixture itself does not require a
POLYGON_API_KEY — the RecordedPolygonFixtureProvider replays bars.json.
"""

from __future__ import annotations

import csv
import json
import os
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]  # PythonDataService/
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "polygon_capture"


def _pick_fixture() -> Path:
    """Find the single committed parity fixture.

    Currently the parity test pins to one window; if multiple fixtures
    exist, fail loudly so the test is unambiguous.
    """
    candidates = sorted(d for d in FIXTURE_ROOT.iterdir()
                        if d.is_dir() and (d / "metadata.json").exists())
    if not candidates:
        pytest.skip(f"no Polygon fixture committed under {FIXTURE_ROOT}")
    if len(candidates) > 1:
        names = ", ".join(c.name for c in candidates)
        raise RuntimeError(
            f"parity test expects exactly one fixture; found {len(candidates)}: {names}"
        )
    return candidates[0]


def _ms_at_session_open(d: date) -> int:
    """09:30 ET of date d, expressed as int64 ms UTC."""
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    dt = datetime(d.year, d.month, d.day, 9, 30, tzinfo=et)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


@pytest.mark.skipif(
    not os.environ.get("LEAN_LAUNCHER_URL"),
    reason="LEAN_LAUNCHER_URL unset; integration test requires the sidecar launcher",
)
@pytest.mark.asyncio
async def test_lean_and_engine_agree_on_polygon_fixture(monkeypatch) -> None:
    from app.lean_sidecar import polygon_canonical
    from app.services.lean_sidecar_service import (
        TrustedRunRequest, run_trusted_sample,
    )
    from app.engine.data.lean_format import LeanMinuteDataReader
    from app.engine.engine import BacktestEngine
    from app.engine.execution.fill_model import FillModel
    from app.engine.execution.order import FillMode
    from app.engine.strategy.algorithms.spy_ema_crossover import SpyEmaCrossoverAlgorithm

    from tests._helpers.parity import (
        assert_state_traces_match, assert_trade_equivalence,
    )

    fixture_dir = _pick_fixture()
    meta = json.loads((fixture_dir / "metadata.json").read_text())
    symbol = meta["symbol"]
    from_date = date.fromisoformat(meta["from_date"])
    to_date = date.fromisoformat(meta["to_date"])

    assert meta.get("observed_trade_count", 0) >= 1, (
        f"fixture {fixture_dir.name} has observed_trade_count="
        f"{meta.get('observed_trade_count')!r}; cannot serve as parity receipt"
    )

    # Inject the fixture provider for the LEAN run.
    fixture_provider = polygon_canonical.RecordedPolygonFixtureProvider(fixture_dir)
    monkeypatch.setattr(
        polygon_canonical, "get_default_provider", lambda: fixture_provider,
    )

    # ── Run LEAN ──
    run_id = f"parity-{uuid.uuid4().hex[:8]}"
    # end_ms_utc is session-open of the day AFTER to_date (half-open window
    # per the P2.5 contract; the router validator expects this shape).
    from datetime import timedelta
    request = TrustedRunRequest(
        run_id=run_id,
        symbol=symbol,
        start_ms_utc=_ms_at_session_open(from_date),
        end_ms_utc=_ms_at_session_open(to_date + timedelta(days=1)),
        starting_cash=100_000.0,
        template="ema_crossover",
        data_source="polygon",
        bar_minutes=15,
        session="regular",
        adjustment="raw",
    )
    result = await run_trusted_sample(request)
    assert result.exit_code == 0, f"LEAN exited non-zero: {result.log_tail[-500:]}"

    # ── Parse LEAN state.csv ──
    state_csv = result.workspace_root / "output" / "storage" / "state.csv"
    assert state_csv.exists(), f"LEAN did not emit state.csv at {state_csv}"
    lean_rows = []
    with state_csv.open() as f:
        for r in csv.DictReader(f):
            lean_rows.append({
                "ts_ms_utc": int(r["ts_ms_utc"]),
                "close": float(r["close"]),
                "ema_fast": float(r["ema_fast"]),
                "ema_slow": float(r["ema_slow"]),
                "rsi": float(r["rsi"]),
                "cross_state": r["cross_state"],
                "signal": r["signal"],
            })

    # ── Run engine over the SAME staged zips ──
    reader = LeanMinuteDataReader(result.workspace_root / "data")
    algo = SpyEmaCrossoverAlgorithm(symbol=symbol)
    captured = []
    orig_handler = algo._on_fifteen_minute_bar

    def recording_handler(bar):
        orig_handler(bar)
        snap = algo.last_decision_snapshot
        if snap is not None:
            captured.append({
                "ts_ms_utc": snap.bar_close_ms,
                "close": float(snap.intended_price),
                "ema_fast": float(snap.ema5),
                "ema_slow": float(snap.ema10),
                "rsi": float(snap.rsi),
                "cross_state": "above" if snap.ema5 > snap.ema10
                               else "below" if snap.ema5 < snap.ema10 else "equal",
                "signal": snap.signal,
            })

    algo._on_fifteen_minute_bar = recording_handler

    # Pin the engine's window + cash to match the LEAN run.
    orig_init = algo.initialize
    def pinned_init():
        orig_init()
        algo.set_start_date(from_date.year, from_date.month, from_date.day)
        algo.set_end_date(to_date.year, to_date.month, to_date.day)
        algo.set_cash(100_000.0)
    algo.initialize = pinned_init

    engine = BacktestEngine(
        data_source=reader,
        fill_model=FillModel(mode=FillMode.SIGNAL_BAR_CLOSE,
                             commission_per_order=Decimal("0")),
    )
    engine.run(algo)

    # ── Assert state-trace parity ──
    assert_state_traces_match(lean_rows, captured, atol=1e-9, rtol=0.0)

    # ── Parse LEAN trades and engine trades ──
    lean_trades = _lean_trades_from_normalized(result.normalized)
    engine_trades = _engine_trades_from_strategy(algo)

    assert_trade_equivalence(lean_trades, engine_trades, fill_price_atol=Decimal("0.01"))


def _lean_trades_from_normalized(normalized) -> list[dict]:
    """Pair LEAN's normalized order events into round-trip trades."""
    # Re-uses the pairing logic from lean_sidecar_persistence.pair_order_events
    # which is already battle-tested for this template. Returns the same dict
    # shape assert_trade_equivalence expects.
    from app.services.lean_sidecar_persistence import pair_order_events

    paired, open_lot = pair_order_events(normalized.order_events)
    assert open_lot is None, "LEAN ended with an unmatched open lot; check OnEndOfAlgorithm"
    return [
        {
            "entry_ms_utc": t.entry_ms_utc,
            "exit_ms_utc": t.exit_ms_utc,
            "quantity": Decimal(str(t.quantity)),
            "entry_price": Decimal(str(t.entry_price)),
            "exit_price": Decimal(str(t.exit_price)),
        }
        for t in paired
    ]


def _engine_trades_from_strategy(algo) -> list[dict]:
    """Translate the engine's LoggedTrade stream into the parity-helper shape."""
    return [
        {
            "entry_ms_utc": int(t.entry_time.timestamp() * 1000),
            "exit_ms_utc": int(t.exit_time.timestamp() * 1000),
            # SpyEmaCrossoverAlgorithm logs trades without quantity; pull
            # it from the portfolio's fill history. For the EMA template
            # the entry quantity is fixed by SetHoldings(1.0); compute it
            # the same way LEAN does: floor(cash / entry_price).
            "quantity": Decimal(int(Decimal(100_000) / t.entry_price)),
            "entry_price": Decimal(str(t.entry_price)),
            "exit_price": Decimal(str(t.exit_price)),
        }
        for t in algo.trade_log
    ]
```

- [ ] **Step 2: Run the parity test**

Ensure LEAN launcher is running (see `.claude/CLAUDE.md` for the command), then:

Run: `podman exec polygon-data-service python -m pytest tests/integration/test_lean_engine_polygon_parity.py -v`

Expected: PASS. If state-trace assertion fails, the helper prints both sides' first divergent row — diagnose accordingly.

- [ ] **Step 3: Commit the test**

```bash
ruff check PythonDataService/tests/integration/
git add PythonDataService/tests/integration/
git commit -m "$(cat <<'EOF'
test(integration): LEAN-vs-engine parity receipt on Polygon fixture

The receipt test. Runs LEAN sidecar in polygon mode against the
committed fixture, then runs SpyEmaCrossoverAlgorithm over the same
staged LEAN zips via LeanMinuteDataReader. Asserts state.csv ≡
DecisionSnapshot stream (atol=1e-9 on EMA/RSI) and trade-by-trade
equivalence (entry/exit price within $0.01).

Skipped without LEAN_LAUNCHER_URL.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 13: Fixture-freshness canary

**Files:**
- Create: `PythonDataService/tests/slow/__init__.py` (if missing)
- Create: `PythonDataService/tests/slow/test_polygon_fixture_freshness.py`

Catches the case where Polygon amends the historical bars (rare but observed in practice).

- [ ] **Step 1: Create the canary test**

Create `PythonDataService/tests/slow/test_polygon_fixture_freshness.py`:

```python
"""Canary: live Polygon ≡ committed fixture.

Re-fetches the parity fixture's window from live Polygon and asserts
byte-equivalence against the committed bars.json. Catches the case
where Polygon amends historical bars (rare but observed); a failure
here means the fixture needs regeneration with a justification commit.

@pytest.mark.slow — opt in via ``pytest -m slow``.
Requires POLYGON_API_KEY.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "polygon_capture"


def _pick_fixture() -> Path:
    candidates = sorted(d for d in FIXTURE_ROOT.iterdir()
                        if d.is_dir() and (d / "metadata.json").exists())
    if not candidates:
        pytest.skip(f"no Polygon fixture committed under {FIXTURE_ROOT}")
    if len(candidates) > 1:
        raise RuntimeError("freshness canary expects exactly one fixture")
    return candidates[0]


@pytest.mark.slow
@pytest.mark.skipif(
    not os.environ.get("POLYGON_API_KEY"),
    reason="POLYGON_API_KEY unset; freshness canary needs live Polygon access",
)
def test_polygon_fixture_matches_live_refetch() -> None:
    from app.lean_sidecar.polygon_canonical import PolygonProvider
    from app.services.polygon_client import PolygonClientService

    fixture_dir = _pick_fixture()
    meta = json.loads((fixture_dir / "metadata.json").read_text())

    provider = PolygonProvider(polygon=PolygonClientService())
    live = provider.fetch_minute_bars(
        symbol=meta["symbol"],
        start_date=date.fromisoformat(meta["from_date"]),
        end_date=date.fromisoformat(meta["to_date"]),
        adjusted=meta["adjusted"],
    )

    live_json = json.dumps(live, separators=(",", ":"))
    live_sha = hashlib.sha256(live_json.encode("utf-8")).hexdigest()

    assert live_sha == meta["bars_sha256"], (
        f"Polygon refetch sha256 ({live_sha[:12]}...) differs from fixture "
        f"({meta['bars_sha256'][:12]}...). Polygon may have amended the data. "
        f"Regenerate the fixture with scripts/regenerate_polygon_fixture.py "
        f"and explain in the commit message."
    )
```

- [ ] **Step 2: Lint and commit (the canary is not run by default CI; PR exercises it via opt-in)**

```bash
ruff check PythonDataService/tests/slow/
git add PythonDataService/tests/slow/
git commit -m "$(cat <<'EOF'
test(slow): Polygon fixture freshness canary

Re-fetches the parity fixture's window from live Polygon and asserts
SHA-256 equivalence against the committed bars.json. Catches Polygon-
side amendments without making routine builds dependent on network.

Opt in via ``pytest -m slow`` with POLYGON_API_KEY set.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 14: Project-scope checks and PR readiness

- [ ] **Step 1: Run project-scope ruff**

Run: `ruff check PythonDataService/app/ PythonDataService/tests/ PythonDataService/scripts/`

Expected: no warnings. Fix anything that surfaces (typically: unused imports left after a refactor, sort-order broken by a new import).

- [ ] **Step 2: Run the full Python test suite**

Run: `podman exec polygon-data-service python -m pytest tests/ -v -k "not slow"`

Expected: all PASS. Compare against a `master`-baseline run to distinguish your work's failures from inherited ones (see `.claude/rules/testing.md` § "Pre-push test-suite hygiene").

- [ ] **Step 3: Manual smoke — LEAN sidecar happy path with a Polygon run**

If the launcher is reachable, run a one-off through the FastAPI router:

```bash
curl -X POST http://localhost:8000/api/lean-sidecar/trusted-runs \
  -H 'Content-Type: application/json' \
  -d '{
    "run_id": "smoke-polygon-1",
    "symbol": "SPY",
    "start_ms_utc": 1736175600000,
    "end_ms_utc": 1736607600000,
    "starting_cash": 100000,
    "template": "ema_crossover",
    "data_source": "polygon",
    "bar_minutes": 15,
    "session": "regular",
    "adjustment": "raw"
  }'
```

Expected: `200 OK` with a populated `TrustedRunResult`. Inspect `<workspace>/manifest.json` and confirm `data_policy.source == "polygon"`. (This step requires POLYGON_API_KEY in the python-service container env if you don't monkey-patch the provider.)

- [ ] **Step 4: Push the branch and open the PR**

```bash
git push -u origin feat/backfill-lean-runs
gh pr create --title "feat(lean-sidecar): LEAN ↔ engine parity on Polygon-sourced bars" --body "$(cat <<'EOF'
## Summary

- Grounds the LEAN-sidecar EMA-crossover validation plane: both engines now consume canonical Polygon 1-minute RTH bars via the same staged LEAN zips.
- Adds `data_source: Literal["synthetic", "polygon"]` to `TrustedRunRequest` / `TrustedRunRequestModel`; synthetic path unchanged.
- New `polygon_canonical` provider (Protocol seam: `PolygonProvider` for prod, `RecordedPolygonFixtureProvider` for tests). Fail-fast on duplicate or non-monotonic Polygon timestamps.
- LEAN EMA template reads symbol/bar_minutes/session/adjustment via GetParameter, drops wall-clock SetWarmUp (indicator-readiness only on both sides), emits `observations.csv` + `state.csv`.
- Run manifest gains `data_policy` sub-block; schema bumps `2 → 3`. `adjusted ⇔ Raw` assertion enforced at manifest construction.
- Receipt test: `tests/integration/test_lean_engine_polygon_parity.py` runs LEAN sidecar, then runs `SpyEmaCrossoverAlgorithm` over the same staged zips via `LeanMinuteDataReader`, asserts state.csv ≡ DecisionSnapshot stream (atol=1e-9) and trade-by-trade equivalence ($0.01).
- Recorded fixture under `tests/fixtures/polygon_capture/` + freshness canary at `tests/slow/`.

Explicitly out of scope: `engine_persistence.py` / `spec_strategy_runner.py` (untracked WIP; conflict with the backend's `source='lean-sidecar'` gate, follow-up branch).

## Test plan

- [ ] `ruff check PythonDataService/app/ PythonDataService/tests/` passes
- [ ] Full Python suite passes: `podman exec polygon-data-service python -m pytest tests/ -v -k "not slow"`
- [ ] Parity test passes: `podman exec polygon-data-service python -m pytest tests/integration/test_lean_engine_polygon_parity.py -v` (requires LEAN_LAUNCHER_URL)
- [ ] Freshness canary passes locally: `podman exec polygon-data-service python -m pytest -m slow tests/slow/test_polygon_fixture_freshness.py -v` (requires POLYGON_API_KEY)
- [ ] Synthetic-path tests still pass (regression check that buy-and-hold and reconciliation templates are byte-identical)

Spec: `docs/superpowers/specs/2026-05-19-lean-engine-polygon-parity-design.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Spec coverage check (self-review)

Verified against `docs/superpowers/specs/2026-05-19-lean-engine-polygon-parity-design.md`:

| Spec section | Implementing task(s) |
|---|---|
| Provider seam (Protocol + Polygon + Fixture + factory) | Tasks 1, 2 |
| `fetch_canonical_minute_bars` + RTH filter + fail-fast | Task 3 |
| Manifest `BarsSpec` + `DataPolicyManifest` + schema bump 2→3 | Task 4 |
| `TrustedRunRequest` 4 new fields | Task 5 |
| Router 4 new fields + constructor wiring | Task 6 |
| EMA template: GetParameter for new params, remove SetWarmUp, observations.csv + state.csv, _to_ms_utc | Task 7 |
| `run_trusted_sample` Polygon branch, LeanConfig params, `data_policy` populating, raw≡Raw assertion | Task 8 |
| `regenerate_polygon_fixture.py` operator script | Task 9 |
| Fixture capture (operator) + window-acceptance gate | Task 10 |
| Parity helpers (`assert_state_traces_match`, `assert_trade_equivalence`) | Task 11 |
| Receipt test (LeanMinuteDataReader over staged zips, state + trade assertion) | Task 12 |
| Freshness canary | Task 13 |
| Project-scope checks + PR | Task 14 |
| `position_qty` excluded from state.csv (asserted in trade test instead) | Task 7 (template) + Task 11 (helpers) |
| `bar_minutes` pinned to `Literal[15]`, defense-in-depth raise in template | Tasks 5, 6, 7 |
| `attribution.md` is human narrative only; provider asserts against `metadata.json` | Tasks 1, 9, 10 |

All spec sections have a task. No placeholders, no `TBD`. Type consistency check: `CanonicalBarsProvider`, `PolygonProvider`, `RecordedPolygonFixtureProvider`, `fetch_canonical_minute_bars`, `get_default_provider`, `BarsSpec`, `DataPolicyManifest`, `_assert_adjustment_vocabulary_consistent`, `assert_state_traces_match`, `assert_trade_equivalence` — all referenced consistently across tasks.
