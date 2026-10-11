"""The decoded-artifact cache: one admitted decode, reused — never the verdict (#3038).

Two contracts live here. The cache itself is bounded, keyed by everything two
reads must agree on, and hands out immutable bars in fresh lists. The readers
that share one across sequential evaluations still run the whole admission
chain on every read — a revoked receipt fails a later read even when that
day's bars sit decoded and resident — and a cached engine run produces
exactly the uncached run's projection.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import settings
from app.data_lake.admission import LakeAdmissionError
from app.data_lake.path_policy import lake_subpath
from app.engine.data.decoded_cache import (
    DECODED_ARTIFACT_SCHEMA_VERSION,
    ArtifactKey,
    DecodedArtifactCache,
    artifact_key,
)
from app.engine.data.lean_format import LeanDailyDataReader, LeanMinuteDataReader
from app.engine.data.trade_bar import TradeBar
from app.research.sweep.snapshot import (
    ManifestBoundMinuteReader,
    capture_data_snapshot,
)
from app.schemas.engine_backtest import EngineBacktestRequest
from app.services.engine_backtest_service import execute_engine_backtest
from tests._helpers.lake_fixture import seed_lake_window
from tests._helpers.lean_store import seed_store_day

DAY = date(2024, 5, 20)
DAY_LATER = date(2024, 5, 21)


def _key(**overrides) -> ArtifactKey:
    base = dict(
        root_id="/lake/root-a",
        root_mode="lake-raw",
        relative_path="equity/usa/minute/spy/20240520_trade.zip",
        content_digest="a" * 64,
        instrument="SPY",
        resolution="minute",
        adjustment_evidence="",
        session_policy="regular",
        calendar_identity="NYSE",
        calendar_version="5.4.0",
        decoder_version=str(DECODED_ARTIFACT_SCHEMA_VERSION),
    )
    return ArtifactKey(**{**base, **overrides})


def _bars(count: int = 3) -> list[TradeBar]:
    return [
        TradeBar(
            symbol="SPY",
            start_ms=1_716_211_800_000 + index * 60_000,
            end_ms=1_716_211_860_000 + index * 60_000,
            open=Decimal("100") + index,
            high=Decimal("101") + index,
            low=Decimal("99") + index,
            close=Decimal("100") + index,
            volume=1_000 + index,
        )
        for index in range(count)
    ]


# -- The cache alone ---------------------------------------------------------


def test_reuse_while_resident_decodes_once_and_hands_out_fresh_lists() -> None:
    cache = DecodedArtifactCache(max_bytes=1024**2)
    decodes = 0

    def decode() -> list[TradeBar]:
        nonlocal decodes
        decodes += 1
        return _bars()

    key = _key()
    first = cache.get_or_decode(key, decode)
    second = cache.get_or_decode(key, decode)

    assert decodes == 1, "a resident decode is reused, not re-decoded"
    assert second == first
    assert second is not first, "each caller gets its own list"
    assert second[0] is first[0], "the bars themselves are shared immutable objects"
    metrics = cache.metrics()
    assert metrics["hits"] == 1 and metrics["misses"] == 1
    assert metrics["uncached_decodes"] == 0 and metrics["evictions"] == 0


def test_one_evaluation_cannot_mutate_the_bars_another_sees() -> None:
    import dataclasses

    cache = DecodedArtifactCache(max_bytes=1024**2)
    key = _key()
    cache.get_or_decode(key, lambda: _bars(5))

    consumed = cache.get_or_decode(key, lambda: _bars(5))
    consumed.pop()  # a consumer mutating its list must not reach the entry
    consumed[0] = _bars(1)[0]

    assert cache.get_or_decode(key, lambda: _bars(5)) == _bars(5)
    with pytest.raises(dataclasses.FrozenInstanceError):
        _bars(1)[0].open = Decimal("1")


@pytest.mark.parametrize(
    "variant",
    [
        {"root_id": "/lake/root-b"},
        {"root_mode": "lake-adjusted"},
        {"relative_path": "equity/usa/minute/spy/20240521_trade.zip"},
        {"content_digest": "b" * 64},
        {"instrument": "QQQ"},
        {"resolution": "daily"},
        {"adjustment_evidence": "c" * 64},
        {"session_policy": "extended"},
        {"calendar_identity": "LSE"},
        {"calendar_version": "9.9.9"},
        {"decoder_version": "2"},
    ],
)
def test_every_key_discriminator_prevents_incompatible_reuse(variant) -> None:
    """Raw/adjusted, regular/extended, other roots or decoders never collide."""
    cache = DecodedArtifactCache(max_bytes=1024**2)
    decodes = 0

    def decode() -> list[TradeBar]:
        nonlocal decodes
        decodes += 1
        return _bars()

    cache.get_or_decode(_key(), decode)
    cache.get_or_decode(_key(**variant), decode)

    assert decodes == 2, f"a differing {next(iter(variant))} must not reuse the decode"


def test_lru_eviction_keeps_residency_bounded() -> None:
    """A hit refreshes use, so eviction takes the least-recently-used entry — not the oldest stored."""
    entry = 64 + 800  # one one-bar entry
    bound = 2 * entry  # exactly two resident
    cache = DecodedArtifactCache(max_bytes=bound)

    oldest = _key(content_digest="1" * 64)
    newest = _key(content_digest="2" * 64)
    cache.get_or_decode(oldest, lambda: _bars(1))
    cache.get_or_decode(newest, lambda: _bars(1))
    cache.get_or_decode(oldest, lambda: _bars(1))  # refresh the older-stored entry
    cache.get_or_decode(_key(content_digest="3" * 64), lambda: _bars(1))

    metrics = cache.metrics()
    assert metrics["evictions"] == 1, "the third entry evicts exactly one"
    assert metrics["resident_entries"] == 2 and metrics["resident_bytes"] <= bound
    assert metrics["uncached_decodes"] == 0

    decodes = 0

    def decode() -> list[TradeBar]:
        nonlocal decodes
        decodes += 1
        return _bars(1)

    cache.get_or_decode(oldest, decode)  # refreshed: still resident
    assert decodes == 0, "the touched entry survived; FIFO would have evicted it"
    cache.get_or_decode(newest, decode)  # not refreshed: evicted
    assert decodes == 1
    assert cache.metrics()["resident_bytes"] <= bound


def test_eviction_skips_pinned_and_takes_the_next_unpinned_victim() -> None:
    entry = 64 + 800
    cache = DecodedArtifactCache(max_bytes=2 * entry)

    pinned_key = _key(content_digest="1" * 64)
    unpinned = _key(content_digest="2" * 64)
    cache.get_or_decode(pinned_key, lambda: _bars(1))
    cache.get_or_decode(unpinned, lambda: _bars(1))
    cache.pin(pinned_key)
    cache.get_or_decode(_key(content_digest="3" * 64), lambda: _bars(1))

    metrics = cache.metrics()
    assert metrics["evictions"] == 1, "the unpinned entry is the victim"
    decodes = 0

    def decode() -> list[TradeBar]:
        nonlocal decodes
        decodes += 1
        return _bars(1)

    cache.get_or_decode(pinned_key, decode)
    assert decodes == 0, "the pinned entry survived the eviction"
    cache.get_or_decode(unpinned, decode)
    assert decodes == 1, "the unpinned entry is the one that was evicted"


def test_pinned_entries_are_skipped_and_overflow_takes_the_uncached_path() -> None:
    bound = 64 + 3 * 800
    cache = DecodedArtifactCache(max_bytes=bound)
    pinned_key = _key(content_digest="1" * 64)
    cache.get_or_decode(pinned_key, lambda: _bars(3))
    cache.pin(pinned_key)

    fresh = cache.get_or_decode(_key(content_digest="2" * 64), lambda: _bars(3))

    assert len(fresh) == 3, "the decode is served even when it cannot be retained"
    metrics = cache.metrics()
    assert metrics["uncached_decodes"] == 1 and metrics["evictions"] == 0
    assert metrics["resident_bytes"] <= bound and metrics["pinned_bytes"] > 0
    cache.unpin(pinned_key)
    assert cache.metrics()["pinned_bytes"] == 0


def test_an_oversized_artifact_takes_the_uncached_path() -> None:
    cache = DecodedArtifactCache(max_bytes=800)

    fresh = cache.get_or_decode(_key(), lambda: _bars(3))

    assert len(fresh) == 3
    metrics = cache.metrics()
    assert metrics["resident_entries"] == 0 and metrics["resident_bytes"] == 0
    assert metrics["uncached_decodes"] == 1 and metrics["misses"] == 1


def test_duplicate_residency_is_reported_honestly() -> None:
    """The same admitted content under two roots holds twice — and says so."""
    cache = DecodedArtifactCache(max_bytes=1024**2)
    digest = "d" * 64
    cache.get_or_decode(_key(root_id="/lake/root-a", content_digest=digest), lambda: _bars(3))
    cache.get_or_decode(_key(root_id="/lake/root-b", content_digest=digest), lambda: _bars(3))

    assert cache.metrics()["duplicate_residency_bytes"] == 2 * (64 + 3 * 800)


def test_the_key_names_the_root_mode_and_relative_path(tmp_path: Path) -> None:
    raw = tmp_path / lake_subpath("raw")
    artifact = raw / "equity" / "usa" / "minute" / "spy" / "20240520_trade.zip"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"zip")

    key = artifact_key(
        artifact_path=artifact,
        data_roots=[tmp_path / "elsewhere", raw],
        content_digest="e" * 64,
        instrument="spy",
        resolution="minute",
        adjustment_evidence="",
        session_policy="regular",
    )
    reference_root = tmp_path / "elsewhere"
    reference_key = artifact_key(
        artifact_path=reference_root / "equity" / "usa" / "minute" / "spy" / "20240520_trade.zip",
        data_roots=[reference_root],
        content_digest="e" * 64,
        instrument="SPY",
        resolution="minute",
        adjustment_evidence="",
        session_policy="regular",
    )
    assert key.root_mode == "lake-raw"
    assert key.relative_path == "equity/usa/minute/spy/20240520_trade.zip"
    assert key.instrument == "SPY"
    assert replace(reference_key, root_mode="lake-raw", root_id=key.root_id) == key, (
        "a reference tree and a lake root holding the same digest stay distinct"
    )


# -- Readers sharing one cache across evaluations -----------------------------


@pytest.fixture
def lake(tmp_path: Path, seeded_lake_catalog) -> Path:
    root = tmp_path / lake_subpath("raw")
    seed_lake_window(root, "SPY", [DAY, DAY_LATER])
    return root


def _snapshot(root: Path):
    return capture_data_snapshot(roots=[root], symbol="SPY", resolution="minute", data_start=DAY, data_end=DAY_LATER)


def _counting_decodes(monkeypatch, reader) -> dict[str, int]:
    """Count one reader's decodes (instance seam) and every admission (module seams).

    ``read_committed_bytes`` is resolved in the module that defines the
    reader's read path — ``lean_format`` for the base readers, ``snapshot``
    for the manifest-bound ones — so both bindings are wrapped and each read
    counts exactly once.
    """
    import app.engine.data.lean_format as lean_format
    import app.research.sweep.snapshot as sweep_snapshot

    counts = {"decode": 0, "admit": 0}
    real_decode = reader.parse_day_zip

    def counting_decode(payload, symbol, trading_date):
        counts["decode"] += 1
        return real_decode(payload, symbol, trading_date)

    def wrap_admit(real_admit):
        def counting_admit(path):
            counts["admit"] += 1
            return real_admit(path)

        return counting_admit

    monkeypatch.setattr(reader, "parse_day_zip", counting_decode)
    monkeypatch.setattr(lean_format, "read_committed_bytes", wrap_admit(lean_format.read_committed_bytes))
    monkeypatch.setattr(sweep_snapshot, "read_committed_bytes", wrap_admit(sweep_snapshot.read_committed_bytes))
    return counts


def test_overlapping_evaluations_reuse_decodes_but_rerun_every_admission(
    lake, seeded_lake_catalog, monkeypatch
) -> None:
    """Two evaluations with different windows decode each shared artifact once; each read still admits."""
    from app.utils.session_anchors import et_date_at_ms

    snapshot = _snapshot(lake)
    cache = DecodedArtifactCache(max_bytes=1024**3)

    first = ManifestBoundMinuteReader(lake, snapshot.artifacts, decoded_cache=cache)
    second = ManifestBoundMinuteReader(lake, snapshot.artifacts, decoded_cache=cache)

    # Evaluation one: the full window. Evaluation two: a different, overlapping
    # window (the later day alone) — a shorter run-up, same admitted artifacts.
    # Separate patch contexts: both counters patch the same module attribute.
    with monkeypatch.context() as first_patch:
        first_counts = _counting_decodes(first_patch, first)
        run_one = list(first.iter_bars("SPY", DAY, DAY_LATER))
    with monkeypatch.context() as second_patch:
        second_counts = _counting_decodes(second_patch, second)
        run_two = list(second.iter_bars("SPY", DAY_LATER, DAY_LATER))

    assert run_one and run_two
    assert run_two == [bar for bar in run_one if et_date_at_ms(bar.start_ms) == DAY_LATER]
    assert first_counts["decode"] == 2, "one decode per artifact while resident"
    assert second_counts["decode"] == 0, "the second evaluation reuses every shared day's decode"
    # Every read of every evaluation admitted again: 2 days then the overlapping 1.
    assert first_counts["admit"] == 2 and second_counts["admit"] == 1
    metrics = cache.metrics()
    assert metrics["misses"] == 2 and metrics["hits"] == 1 and metrics["uncached_decodes"] == 0


def test_revoked_receipt_fails_a_later_read_even_when_the_day_is_decoded(lake, seeded_lake_catalog) -> None:
    """Mid-evaluation revocation before a later day's read fails, decoded or not."""
    snapshot = _snapshot(lake)
    cache = DecodedArtifactCache(max_bytes=1024**3)
    warm = ManifestBoundMinuteReader(lake, snapshot.artifacts, decoded_cache=cache)
    assert list(warm.iter_bars("SPY", DAY, DAY_LATER)), "both days decoded and resident"

    seeded_lake_catalog.clear()  # the receipts are revoked; the files remain
    later = ManifestBoundMinuteReader(lake, snapshot.artifacts, decoded_cache=cache)
    with pytest.raises(LakeAdmissionError, match="committed"):
        list(later.iter_bars("SPY", DAY, DAY))
    assert cache.metrics()["hits"] == 0, "no hit was served: admission precedes every reuse"


def test_revocation_mid_iteration_fails_the_next_day_inside_one_walk(lake, seeded_lake_catalog) -> None:
    """Revocation between two days of one evaluation's own walk fails the second read.

    ``iter_bars`` is a ``read_day`` per date; this drives that day loop the
    way the engine does, so the revocation lands between one day's read and
    the next — inside one evaluation, not between two.
    """
    snapshot = _snapshot(lake)
    cache = DecodedArtifactCache(max_bytes=1024**3)
    warm = ManifestBoundMinuteReader(lake, snapshot.artifacts, decoded_cache=cache)
    assert list(warm.iter_bars("SPY", DAY, DAY_LATER)), "both days decoded and resident"

    walking = ManifestBoundMinuteReader(lake, snapshot.artifacts, decoded_cache=cache)
    assert walking.read_day("SPY", DAY), "the first day's bars came from the resident decode"
    seeded_lake_catalog.clear()  # revoked between one day's read and the next
    with pytest.raises(LakeAdmissionError, match="committed"):
        walking.read_day("SPY", DAY_LATER)


def test_daily_evaluations_share_the_history_decode(lake, seeded_lake_catalog, monkeypatch) -> None:
    cache = DecodedArtifactCache(max_bytes=1024**3)
    decodes = 0
    real_parse = LeanDailyDataReader.parse_history_zip

    def counting_parse(self, payload, symbol):
        nonlocal decodes
        decodes += 1
        return real_parse(self, payload, symbol)

    monkeypatch.setattr(LeanDailyDataReader, "parse_history_zip", counting_parse)

    first = LeanDailyDataReader(lake, decoded_cache=cache)
    second = LeanDailyDataReader(lake, decoded_cache=cache)
    bars_one = list(first.iter_bars("SPY", DAY, DAY_LATER))
    bars_two = list(second.iter_bars("SPY", DAY, DAY_LATER))

    assert bars_one == bars_two and bars_one
    assert decodes == 1, "the per-symbol history artifact decodes once while resident"
    metrics = cache.metrics()
    assert metrics["hits"] == 1 and metrics["misses"] == 1


# -- The engine run a researcher actually gets --------------------------------


@pytest.fixture
def reference_store(tmp_path: Path, monkeypatch) -> Path:
    write_root = tmp_path / "writer-root"
    lake_dir = write_root / lake_subpath("raw")
    lake_dir.mkdir(parents=True)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    seed_store_day(lake_dir, "SPY", DAY)
    seed_store_day(lake_dir, "SPY", DAY_LATER)
    return lake_dir


def _engine_request() -> EngineBacktestRequest:
    return EngineBacktestRequest(
        strategy_name="sma_crossover",
        params={"symbol": "SPY"},
        from_date=DAY.isoformat(),
        to_date=DAY_LATER.isoformat(),
        auto_fetch=False,
        data_policy={
            "source": "polygon",
            "symbol": "SPY",
            "adjusted": False,
            "session": "regular",
            "input_bars": {"timespan": "minute", "multiplier": 1},
            "strategy_bars": {"timespan": "minute", "multiplier": 1},
        },
    )


@pytest.mark.usefixtures("seeded_lake_catalog")
def test_a_cached_engine_run_matches_the_uncached_run_exactly(reference_store, monkeypatch) -> None:
    """Speed does not change the scientific answer: one projection, two paths (#3038)."""
    from app.research.backtest_runs.service import SAVE_FAILED
    from app.services import engine_backtest_service as engine_service

    # The .NET study save is best-effort and not what this test is about.
    monkeypatch.setattr(engine_service, "persist_engine_response_sync", lambda **kwargs: SAVE_FAILED)

    def normalized(response) -> dict:
        payload = response.model_dump()
        payload["run_verdict"]["generated_at_ms"] = 0  # the one wall-clock field two runs may differ in
        return payload

    plain = execute_engine_backtest(request=_engine_request(), on_phase=lambda phase: None, on_log=lambda message: None)
    assert plain.success, plain.error

    cache = DecodedArtifactCache(max_bytes=1024**3)
    warm = execute_engine_backtest(
        request=_engine_request(), on_phase=lambda phase: None, on_log=lambda message: None, decoded_cache=cache
    )
    assert warm.success, warm.error
    assert normalized(warm) == normalized(plain), "a cached run is the uncached run"

    # One engine run reads each artifact exactly once, so reuse shows on the
    # next evaluation sharing the cache — a second run of the same request.
    repeat = execute_engine_backtest(
        request=_engine_request(), on_phase=lambda phase: None, on_log=lambda message: None, decoded_cache=cache
    )
    assert repeat.success, repeat.error
    assert normalized(repeat) == normalized(plain)
    assert cache.metrics()["hits"] > 0, "the warm path actually reused admitted decodes"

    # And bar for bar, not merely equal statistics: every field of every bar.
    uncached_bars = list(LeanMinuteDataReader(reference_store).iter_bars("SPY", DAY, DAY_LATER))
    cached_bars = list(LeanMinuteDataReader(reference_store, decoded_cache=cache).iter_bars("SPY", DAY, DAY_LATER))
    assert [vars(bar) for bar in cached_bars] == [vars(bar) for bar in uncached_bars]
