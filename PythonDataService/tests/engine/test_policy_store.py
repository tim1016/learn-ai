"""Tests for the policy-keyed canonical bar store (app.engine.data.policy_store)."""

from __future__ import annotations

import json
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from app.engine.data.availability import ensure_range
from app.engine.data.policy_store import (
    policy_key,
    read_provenance,
    record_fetch,
    resolve_cache_root,
    resolve_data_roots,
    resolve_policy_root,
    snapshot_minute_trade_zips,
    symbol_write_lock,
)
from app.lean_sidecar.workspace import SymbolValidationError

FETCHED_AT_MS = 1783958400000


def test_policy_key_encodes_source_and_adjustment():
    assert policy_key(source="polygon", adjusted=True) == "polygon-adjusted"
    assert policy_key(source="polygon", adjusted=False) == "polygon-raw"


def test_resolve_policy_root_nests_under_cache_root(tmp_path: Path):
    root = resolve_policy_root(source="polygon", adjusted=False, cache_root=tmp_path)
    assert root == tmp_path / "polygon-raw"


def test_resolve_cache_root_honors_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LEAN_DATA_CACHE", str(tmp_path / "store"))
    assert resolve_cache_root() == tmp_path / "store"


def test_resolve_data_roots_reference_first_and_creates_policy_root(monkeypatch, tmp_path: Path):
    ref = tmp_path / "reference"
    ref.mkdir()
    monkeypatch.setenv("LEAN_DATA_ROOT", str(ref))
    monkeypatch.setenv("LEAN_DATA_CACHE", str(tmp_path / "store"))

    roots = resolve_data_roots(source="polygon", adjusted=True)

    assert roots == [ref, tmp_path / "store" / "polygon-adjusted"]
    assert roots[1].is_dir()


def test_resolve_data_roots_skips_missing_reference(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("LEAN_DATA_ROOT", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("LEAN_DATA_CACHE", str(tmp_path / "store"))

    roots = resolve_data_roots(source="polygon", adjusted=False)

    assert roots == [tmp_path / "store" / "polygon-raw"]


def test_snapshot_minute_trade_zips_is_path_independent_and_reference_first(tmp_path: Path):
    reference = tmp_path / "reference"
    cache = tmp_path / "cache"
    logical = Path("equity/usa/minute/spy")
    (reference / logical).mkdir(parents=True)
    (cache / logical).mkdir(parents=True)
    (reference / logical / "20260105_trade.zip").write_bytes(b"reference-day-one")
    (cache / logical / "20260105_trade.zip").write_bytes(b"shadowed-cache-day-one")
    (cache / logical / "20260106_trade.zip").write_bytes(b"cache-day-two")

    receipt = snapshot_minute_trade_zips(
        [reference, cache],
        symbol="SPY",
        start=date(2026, 1, 5),
        end=date(2026, 1, 6),
        adjusted=False,
        session="regular",
    )

    assert receipt["fixture_id"].startswith("bar-store-v1-")
    assert len(receipt["fixture_sha256"]) == 64
    assert [item["path"] for item in receipt["files"]] == [
        "equity/usa/minute/spy/20260105_trade.zip",
        "equity/usa/minute/spy/20260106_trade.zip",
    ]
    assert receipt["files"][0]["size_bytes"] == len(b"reference-day-one")

    relocated_reference = tmp_path / "elsewhere" / "reference"
    relocated_cache = tmp_path / "elsewhere" / "cache"
    (relocated_reference / logical).mkdir(parents=True)
    (relocated_cache / logical).mkdir(parents=True)
    (relocated_reference / logical / "20260105_trade.zip").write_bytes(b"reference-day-one")
    (relocated_cache / logical / "20260106_trade.zip").write_bytes(b"cache-day-two")
    relocated = snapshot_minute_trade_zips(
        [relocated_reference, relocated_cache],
        symbol="SPY",
        start=date(2026, 1, 5),
        end=date(2026, 1, 6),
        adjusted=False,
        session="regular",
    )
    assert relocated["fixture_sha256"] == receipt["fixture_sha256"]


def test_snapshot_minute_trade_zips_changes_when_one_input_byte_changes(tmp_path: Path):
    logical = tmp_path / "equity/usa/minute/spy"
    logical.mkdir(parents=True)
    path = logical / "20260105_trade.zip"
    path.write_bytes(b"first")
    before = snapshot_minute_trade_zips(
        [tmp_path],
        symbol="SPY",
        start=date(2026, 1, 5),
        end=date(2026, 1, 5),
        adjusted=False,
        session="regular",
    )
    path.write_bytes(b"second")
    after = snapshot_minute_trade_zips(
        [tmp_path],
        symbol="SPY",
        start=date(2026, 1, 5),
        end=date(2026, 1, 5),
        adjusted=False,
        session="regular",
    )

    assert after["fixture_sha256"] != before["fixture_sha256"]


def test_snapshot_minute_trade_zips_rejects_path_unsafe_symbol(tmp_path: Path):
    with pytest.raises(SymbolValidationError):
        snapshot_minute_trade_zips(
            [tmp_path],
            symbol="../evil",
            start=date(2026, 1, 5),
            end=date(2026, 1, 5),
            adjusted=False,
            session="regular",
        )


def test_snapshot_minute_trade_zips_ignores_symlink_that_escapes_root(tmp_path: Path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    symbol_parent = root / "equity" / "usa" / "minute"
    symbol_parent.mkdir(parents=True)
    outside.mkdir()
    (outside / "20260105_trade.zip").write_bytes(b"outside-root")
    (symbol_parent / "spy").symlink_to(outside, target_is_directory=True)

    with pytest.raises(FileNotFoundError, match="no minute trade zips"):
        snapshot_minute_trade_zips(
            [root],
            symbol="SPY",
            start=date(2026, 1, 5),
            end=date(2026, 1, 5),
            adjusted=False,
            session="regular",
        )


def test_record_fetch_creates_and_appends(tmp_path: Path):
    record_fetch(
        tmp_path,
        "SPY",
        source="polygon",
        adjusted=False,
        resolution="minute",
        from_date="2026-01-05",
        to_date="2026-01-06",
        fetched_at_ms=FETCHED_AT_MS,
    )
    record_fetch(
        tmp_path,
        "SPY",
        source="polygon",
        adjusted=False,
        resolution="daily",
        from_date="2026-01-05",
        to_date="2026-02-27",
        fetched_at_ms=FETCHED_AT_MS + 1,
    )

    doc = read_provenance(tmp_path, "SPY")
    assert doc is not None
    assert doc["schema_version"] == 1
    assert doc["policy"] == {"source": "polygon", "adjusted": False}
    assert [f["resolution"] for f in doc["fetches"]] == ["minute", "daily"]
    assert doc["fetches"][0]["fetched_at_ms"] == FETCHED_AT_MS


def test_record_fetch_rejects_policy_mismatch(tmp_path: Path):
    record_fetch(
        tmp_path,
        "SPY",
        source="polygon",
        adjusted=False,
        resolution="minute",
        from_date="2026-01-05",
        to_date="2026-01-06",
        fetched_at_ms=FETCHED_AT_MS,
    )

    with pytest.raises(ValueError, match="policy mismatch"):
        record_fetch(
            tmp_path,
            "SPY",
            source="polygon",
            adjusted=True,
            resolution="minute",
            from_date="2026-01-05",
            to_date="2026-01-06",
            fetched_at_ms=FETCHED_AT_MS,
        )


def test_read_provenance_absent_returns_none(tmp_path: Path):
    assert read_provenance(tmp_path, "SPY") is None


def test_symbol_write_lock_rejects_path_unsafe_symbol(tmp_path: Path):
    with pytest.raises(SymbolValidationError), symbol_write_lock(tmp_path, "../evil"):
        pass


def test_read_provenance_rejects_path_unsafe_symbol(tmp_path: Path):
    with pytest.raises(SymbolValidationError):
        read_provenance(tmp_path, "../evil")


def test_record_fetch_rejects_path_unsafe_symbol(tmp_path: Path):
    with pytest.raises(SymbolValidationError):
        record_fetch(
            tmp_path,
            "../evil",
            source="polygon",
            adjusted=False,
            resolution="minute",
            from_date="2025-01-06",
            to_date="2025-01-06",
            fetched_at_ms=FETCHED_AT_MS,
        )


def test_symbol_write_lock_serializes_writers(tmp_path: Path):
    """Two threads contending for the same symbol never overlap."""
    active = 0
    max_active = 0
    guard = threading.Lock()

    def worker() -> None:
        nonlocal active, max_active
        with symbol_write_lock(tmp_path, "SPY"):
            with guard:
                active += 1
                max_active = max(max_active, active)
            # Give the other thread a chance to (incorrectly) enter.
            threading.Event().wait(0.05)
            with guard:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max_active == 1


class _FakePolygon:
    """Deterministic minute-bar source counting how often it is fetched."""

    def __init__(self) -> None:
        self.calls = 0
        self.adjusted_seen: list[bool] = []
        self.ranges_seen: list[tuple[str, str]] = []

    def fetch_aggregates(self, **kwargs) -> list[dict]:
        self.calls += 1
        self.adjusted_seen.append(kwargs["adjusted"])
        self.ranges_seen.append((kwargs["from_date"], kwargs["to_date"]))
        start = date.fromisoformat(kwargs["from_date"])
        end = date.fromisoformat(kwargs["to_date"])
        bars: list[dict] = []
        current = start
        while current <= end:
            if current.weekday() < 5:
                open_ms = int(datetime(current.year, current.month, current.day, 14, 30, tzinfo=UTC).timestamp() * 1000)
                for i in range(30):
                    bars.append(
                        {
                            "timestamp": open_ms + i * 60_000,
                            "open": 500.0,
                            "high": 500.5,
                            "low": 499.5,
                            "close": 500.25,
                            "volume": 1000,
                        }
                    )
            current += timedelta(days=1)
        return bars


def test_ensure_range_writes_provenance_with_policy(tmp_path: Path):
    polygon = _FakePolygon()
    policy_root = tmp_path / "polygon-raw"
    policy_root.mkdir()

    report = ensure_range(
        reference_roots=[],
        cache_root=policy_root,
        symbol="SPY",
        start=date(2026, 1, 5),
        end=date(2026, 1, 6),
        polygon=polygon,
        adjusted=False,
        resolution="minute",
    )

    assert report.is_complete
    assert polygon.adjusted_seen == [False]
    doc = json.loads((policy_root / "provenance" / "spy.json").read_text())
    assert doc["policy"] == {"source": "polygon", "adjusted": False}
    assert doc["fetches"][0]["from_date"] == "2026-01-05"


def test_ensure_range_skips_fetch_when_complete(tmp_path: Path):
    polygon = _FakePolygon()
    policy_root = tmp_path / "polygon-raw"
    policy_root.mkdir()
    window = {"start": date(2026, 1, 5), "end": date(2026, 1, 6)}

    for _ in range(2):
        ensure_range(
            reference_roots=[],
            cache_root=policy_root,
            symbol="SPY",
            polygon=polygon,
            adjusted=False,
            resolution="minute",
            **window,
        )

    assert polygon.calls == 1


def test_ensure_range_concurrent_runs_fetch_once(tmp_path: Path):
    """Two runs racing on the same symbol serialize on the store lock and
    the loser observes the winner's zips instead of re-fetching."""
    polygon = _FakePolygon()
    policy_root = tmp_path / "polygon-raw"
    policy_root.mkdir()
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            ensure_range(
                reference_roots=[],
                cache_root=policy_root,
                symbol="SPY",
                start=date(2026, 1, 5),
                end=date(2026, 1, 6),
                polygon=polygon,
                adjusted=False,
                resolution="minute",
            )
        except Exception as exc:  # surface to the main thread's assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert polygon.calls == 1
    zips = sorted(p.name for p in (policy_root / "equity" / "usa" / "minute" / "spy").glob("*_trade.zip"))
    assert zips == ["20260105_trade.zip", "20260106_trade.zip"]


def test_ensure_range_refetches_only_the_missing_day_not_the_whole_window(tmp_path: Path):
    """Regression for #1830: a window with one missing day out of a
    larger, otherwise-complete range must fetch only that one day from
    the provider — not re-export the entire requested window."""
    polygon = _FakePolygon()
    policy_root = tmp_path / "polygon-raw"
    policy_root.mkdir()
    window = {"start": date(2026, 1, 5), "end": date(2026, 1, 9)}  # Mon-Fri, no holidays

    ensure_range(
        reference_roots=[],
        cache_root=policy_root,
        symbol="SPY",
        polygon=polygon,
        adjusted=False,
        resolution="minute",
        **window,
    )
    assert polygon.calls == 1
    assert polygon.ranges_seen == [("2026-01-05", "2026-01-09")]

    # Simulate a single missing day in the middle of an otherwise-complete
    # window (e.g. a torn/evicted zip).
    zip_dir = policy_root / "equity" / "usa" / "minute" / "spy"
    (zip_dir / "20260107_trade.zip").unlink()

    report = ensure_range(
        reference_roots=[],
        cache_root=policy_root,
        symbol="SPY",
        polygon=polygon,
        adjusted=False,
        resolution="minute",
        **window,
    )

    assert report.is_complete
    assert polygon.calls == 2
    assert polygon.ranges_seen[-1] == ("2026-01-07", "2026-01-07")
    doc = json.loads((policy_root / "provenance" / "spy.json").read_text())
    assert [f["from_date"] for f in doc["fetches"]] == ["2026-01-05", "2026-01-07"]
    assert [f["to_date"] for f in doc["fetches"]] == ["2026-01-09", "2026-01-07"]
    zips = sorted(p.name for p in zip_dir.glob("*_trade.zip"))
    assert zips == [
        "20260105_trade.zip",
        "20260106_trade.zip",
        "20260107_trade.zip",
        "20260108_trade.zip",
        "20260109_trade.zip",
    ]


def test_ensure_range_fetches_disjoint_missing_subranges_separately(tmp_path: Path):
    """A contiguous gap and an isolated gap in the same window produce two
    provider calls and two provenance records — one per delta range, not
    one call per missing day and not one call for the whole window."""
    polygon = _FakePolygon()
    policy_root = tmp_path / "polygon-raw"
    policy_root.mkdir()
    window = {"start": date(2026, 1, 5), "end": date(2026, 1, 9)}

    ensure_range(
        reference_roots=[],
        cache_root=policy_root,
        symbol="SPY",
        polygon=polygon,
        adjusted=False,
        resolution="minute",
        **window,
    )

    zip_dir = policy_root / "equity" / "usa" / "minute" / "spy"
    (zip_dir / "20260106_trade.zip").unlink()
    (zip_dir / "20260107_trade.zip").unlink()
    (zip_dir / "20260109_trade.zip").unlink()

    report = ensure_range(
        reference_roots=[],
        cache_root=policy_root,
        symbol="SPY",
        polygon=polygon,
        adjusted=False,
        resolution="minute",
        **window,
    )

    assert report.is_complete
    assert polygon.ranges_seen[1:] == [("2026-01-06", "2026-01-07"), ("2026-01-09", "2026-01-09")]
    doc = json.loads((policy_root / "provenance" / "spy.json").read_text())
    fetched_ranges = [(f["from_date"], f["to_date"]) for f in doc["fetches"]]
    assert fetched_ranges == [
        ("2026-01-05", "2026-01-09"),
        ("2026-01-06", "2026-01-07"),
        ("2026-01-09", "2026-01-09"),
    ]
    zips = sorted(p.name for p in zip_dir.glob("*_trade.zip"))
    assert zips == [
        "20260105_trade.zip",
        "20260106_trade.zip",
        "20260107_trade.zip",
        "20260108_trade.zip",
        "20260109_trade.zip",
    ]
