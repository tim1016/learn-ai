"""Tests for the multi-snapshot IV recorder (Step D of IV-ownership plan).

Three layers:

1. Pure store contracts (in-memory + JSONL).
2. The recorder service against a mocked Polygon client.
3. The HTTP router with the in-memory store injected.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.options.expiry import option_expiry_anchor_ms
from app.services.iv_recorder import (
    OPTIONS_SNAPSHOT_DELAY_MS,
    InMemoryIvSnapshotStore,
    JsonlIvSnapshotStore,
    RecordedIvSnapshot,
    latest_quote_ms,
    record_iv_snapshot,
)
from tests._helpers.iv_chain import bs_chain_payload

ET = ZoneInfo("America/New_York")


def snapshot_of(
    ticker: str = "SPY", *, ts: int = 1_700_000_000_000, slot: str = "15:55", chain: int = 3, iv: float | None = 0.2, **overrides
) -> RecordedIvSnapshot:
    """A snapshot with a raw chain of ``chain`` contracts (each a few hundred bytes of bid/ask, as a real one is)."""
    fields = {
        "ticker": ticker, "snapshot_ts_ms": ts, "asked_at_ms": ts + OPTIONS_SNAPSHOT_DELAY_MS, "slot": slot, "spot": 400.0,
        "rate": 0.045, "dividend_yield": 0.0, "rate_source": "FRED", "dividend_source": "Polygon TTM",
        "iv30_vix_style": iv, "iv30_parametric": None, "iv_provenance": {"iv_source": "internal_solver"},
        "raw_chain": [
            {"ticker": f"O:{ticker}{i}", "contract_type": "call", "strike_price": 400.0 + i, "expiration_ms": 1, "bid": 1.0, "ask": 1.1,
             "polygon_iv_diagnostic": 0.2}
            for i in range(chain)
        ],
    }
    return RecordedIvSnapshot(**{**fields, **overrides})


class TestJsonlStore:
    def test_the_series_row_names_the_raw_chain_and_the_chain_sits_in_a_file_of_its_own(self, tmp_path: Path):
        store = JsonlIvSnapshotStore(tmp_path)
        quote_ts = int(datetime(2024, 3, 11, 12, 30, tzinfo=ET).timestamp() * 1000)
        snap = snapshot_of("QQQ", ts=quote_ts, slot="12:30", chain=5)

        store.write(snap)

        line = json.loads((tmp_path / "QQQ.jsonl").read_text().strip())
        assert line["ticker"] == "QQQ" and line["snapshot_ts_ms"] == quote_ts
        assert line["asked_at_ms"] == quote_ts + OPTIONS_SNAPSHOT_DELAY_MS
        assert "raw_chain" not in line, "the series carries no chain"
        chain_file = tmp_path / line["raw_chain_path"]
        assert chain_file.is_file() and line["raw_chain_path"].startswith("raw/QQQ/2024-03-11_1230_")
        assert hashlib.sha256(chain_file.read_bytes()).hexdigest() == line["raw_chain_sha256"]

        (row,) = JsonlIvSnapshotStore(tmp_path).read_series("QQQ")
        assert row.snapshot_ts_ms == quote_ts and not hasattr(row, "raw_chain")
        assert JsonlIvSnapshotStore(tmp_path).read_raw_chain(row) == snap.raw_chain, "the bid/ask per contract is kept whole"

    def test_a_row_with_no_chain_writes_no_chain_file_and_reads_back_an_empty_one(self, tmp_path: Path):
        store = JsonlIvSnapshotStore(tmp_path)
        store.write(snapshot_of(raw_chain=[], error="polygon_fetch_failed: 502", iv=None))

        (row,) = store.read_series("SPY")
        assert row.raw_chain_path is None and row.raw_chain_sha256 is None
        assert store.read_raw_chain(row) == []
        assert not (tmp_path / "raw").exists()

    def test_the_chain_is_published_before_the_row_that_names_it_is_appended(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from app.services import iv_recorder

        store = JsonlIvSnapshotStore(tmp_path)
        seen: list[tuple[str, bool]] = []
        publish = iv_recorder.atomic_write_bytes

        def spy(path: Path, data: bytes, **kwargs: object) -> None:
            seen.append((path.parent.name, (tmp_path / "SPY.jsonl").exists()))
            publish(path, data, **kwargs)

        monkeypatch.setattr(iv_recorder, "atomic_write_bytes", spy)
        store.write(snapshot_of())

        assert seen == [("SPY", False)], "the chain file was written while the series had no row for it"
        assert len(store.read_series("SPY")) == 1

    def test_a_crash_between_the_chain_and_its_row_leaves_an_unreferenced_chain_and_a_series_that_reads_as_before(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        store = JsonlIvSnapshotStore(tmp_path)
        store.write(snapshot_of(ts=1_000, chain=2))

        def crash(_path: Path) -> None:
            raise OSError("the process died after the chain was published")

        monkeypatch.setattr("app.services.iv_recorder._end_torn_tail", crash)
        with pytest.raises(OSError, match="the process died"):
            store.write(snapshot_of(ts=2_000, chain=3))
        monkeypatch.undo()

        assert [r.snapshot_ts_ms for r in store.read_series("SPY")] == [1_000], "no row names the orphaned chain"
        assert len(list((tmp_path / "raw" / "SPY").glob("*.json"))) == 2

    def test_reading_a_year_of_rows_touches_no_raw_chain(self, tmp_path: Path):
        store = JsonlIvSnapshotStore(tmp_path)
        for day in range(252):
            store.write(snapshot_of(ts=1_700_000_000_000 + day * 86_400_000, chain=400))
        raw_bytes = sum(f.stat().st_size for f in (tmp_path / "raw").rglob("*.json"))
        series_bytes = (tmp_path / "SPY.jsonl").stat().st_size
        assert series_bytes * 50 < raw_bytes, "the series is a small fraction of what the chains hold"

        shutil.rmtree(tmp_path / "raw")
        rows = store.read_series("SPY")

        assert len(rows) == 252, "every row read with every chain gone: no chain was needed"
        assert rows[0].snapshot_ts_ms == 1_700_000_000_000 and rows[-1].snapshot_ts_ms == 1_700_000_000_000 + 251 * 86_400_000
        assert len(store.read_series("SPY", start_ms=1_700_000_000_000 + 250 * 86_400_000)) == 2

    def test_a_chain_that_is_not_the_one_the_row_names_is_refused(self, tmp_path: Path):
        store = JsonlIvSnapshotStore(tmp_path)
        store.write(snapshot_of())
        (row,) = store.read_series("SPY")
        assert row.raw_chain_path is not None
        (tmp_path / row.raw_chain_path).write_text("[]")

        with pytest.raises(ValueError, match="sha256 differs"):
            store.read_raw_chain(row)

    def test_the_same_slot_asked_twice_in_a_day_keeps_each_chain_under_its_own_digest(self, tmp_path: Path):
        store = JsonlIvSnapshotStore(tmp_path)
        store.write(snapshot_of(ts=1_000, chain=2))
        store.write(snapshot_of(ts=2_000, chain=3))

        first, second = store.read_series("SPY")
        assert first.raw_chain_path != second.raw_chain_path
        assert len(store.read_raw_chain(first)) == 2 and len(store.read_raw_chain(second)) == 3

    def test_unknown_ticker_returns_empty(self, tmp_path: Path):
        store = JsonlIvSnapshotStore(tmp_path)
        assert store.read_series("XYZ") == []

    def test_read_series_refuses_a_ticker_that_escapes_the_store(self, tmp_path: Path):
        # The ticker reaches read_series from a request body (the Realized vs IV job).
        store_dir = tmp_path / "store"
        (tmp_path / "secret.jsonl").write_text("{}\n")
        store = JsonlIvSnapshotStore(store_dir)
        with pytest.raises(ValueError, match="escapes root"):
            store.read_series("../secret")

    def test_write_refuses_a_ticker_that_escapes_the_store_and_writes_nothing(self, tmp_path: Path):
        store = JsonlIvSnapshotStore(tmp_path / "store")
        with pytest.raises(ValueError, match="escapes root"):
            store.write(snapshot_of("../escape"))

        assert not (tmp_path / "escape.jsonl").exists() and not (tmp_path / "store" / "raw").exists()

    def test_a_series_row_that_lacks_the_optional_fields_reads_back_with_their_defaults(self, tmp_path: Path):
        # A row written before health_score, or without a chain: the optional fields default, so older lines still read.
        row = {
            "ticker": "SPY", "snapshot_ts_ms": 1700000000000, "slot": "16:00", "spot": 590.0, "rate": 0.045,
            "dividend_yield": 0.012, "rate_source": "FRED", "dividend_source": "Polygon TTM", "iv30_vix_style": 0.18,
            "iv30_parametric": None, "iv_provenance": {"iv_source": "internal_solver"},
        }
        (tmp_path / "SPY.jsonl").write_text(json.dumps(row) + "\n")

        (read,) = JsonlIvSnapshotStore(tmp_path).read_series("SPY")

        assert read.health_score is None and read.error is None
        assert (read.asked_at_ms, read.quote_ts_derived, read.raw_chain_path) == (None, False, None)
        assert read.iv30_vix_style == pytest.approx(0.18)

    def test_a_torn_last_line_is_skipped_and_logged_and_the_rows_before_it_are_read(self, tmp_path: Path, caplog):
        store = JsonlIvSnapshotStore(tmp_path)
        store.write(snapshot_of(ts=1_000))
        store.write(snapshot_of(ts=2_000))
        with (tmp_path / "SPY.jsonl").open("a") as f:
            f.write('{"ticker":"SPY","snapshot_ts_ms":3000,"slo')

        with caplog.at_level(logging.WARNING, logger="app.services.iv_recorder"):
            rows = store.read_series("SPY")

        assert [r.snapshot_ts_ms for r in rows] == [1_000, 2_000]
        (event,) = [r for r in caplog.records if getattr(r, "action", "") == "iv_series_torn_last_line_skipped"]
        assert event.ticker == "SPY" and event.line == 3

    def test_a_bad_line_that_is_not_the_last_still_fails_loudly(self, tmp_path: Path):
        store = JsonlIvSnapshotStore(tmp_path)
        store.write(snapshot_of(ts=1_000))
        with (tmp_path / "SPY.jsonl").open("a") as f:
            f.write("not json\n")
        store.write(snapshot_of(ts=3_000))

        with pytest.raises(ValueError, match=r"SPY.jsonl line 2 is not JSON, and it is not the last line"):
            store.read_series("SPY")

    def test_a_line_that_is_json_but_not_a_row_fails_loudly_even_when_it_is_last(self, tmp_path: Path):
        (tmp_path / "SPY.jsonl").write_text('{"ticker":"SPY","not_a_field":1}\n')

        with pytest.raises(ValueError, match=r"SPY.jsonl line 1 is not an IV series row"):
            JsonlIvSnapshotStore(tmp_path).read_series("SPY")

    def test_the_next_write_after_a_torn_append_drops_the_fragment_and_loses_no_row(self, tmp_path: Path, caplog):
        store = JsonlIvSnapshotStore(tmp_path)
        store.write(snapshot_of(ts=1_000))
        with (tmp_path / "SPY.jsonl").open("a") as f:
            f.write('{"ticker":"SPY","snapshot_ts_ms":2000,"slo')

        with caplog.at_level(logging.WARNING, logger="app.services.iv_recorder"):
            store.write(snapshot_of(ts=3_000))

        assert [r.snapshot_ts_ms for r in store.read_series("SPY")] == [1_000, 3_000], "no bad line is left in the middle"
        (event,) = [r for r in caplog.records if getattr(r, "action", "") == "iv_series_torn_tail_dropped"]
        assert event.dropped_bytes == len('{"ticker":"SPY","snapshot_ts_ms":2000,"slo')

    def test_a_whole_last_row_that_lost_only_its_line_break_is_kept_and_the_break_added(self, tmp_path: Path, caplog):
        store = JsonlIvSnapshotStore(tmp_path)
        store.write(snapshot_of(ts=1_000))
        series = tmp_path / "SPY.jsonl"
        series.write_text(series.read_text().rstrip("\n"))
        assert len(store.read_series("SPY")) == 1, "a whole row is a row, with or without its break"

        with caplog.at_level(logging.WARNING, logger="app.services.iv_recorder"):
            store.write(snapshot_of(ts=2_000))

        assert [r.snapshot_ts_ms for r in store.read_series("SPY")] == [1_000, 2_000]
        assert [r.action for r in caplog.records if hasattr(r, "action")] == ["iv_series_missing_line_break_added"]


class TestRecorderService:
    def test_writes_full_provenance_on_success(self):
        store = InMemoryIvSnapshotStore()
        polygon = MagicMock()
        spot = 591.0
        sigma = 0.20
        rate = 0.045
        asof = datetime(2026, 4, 28, 13, 35, tzinfo=UTC)
        polygon.list_snapshot_options_chain.return_value = bs_chain_payload(
            spot=spot, sigma=sigma, rate=rate, asof=asof,
            expiry_days=[21, 28, 35, 42],
            strikes=[float(k) for k in range(540, 651, 5)],
        )

        with patch("app.services.iv_recorder.get_rate_and_dividend") as rd:
            from app.services.rate_dividend_service import RateAndDividend
            rd.return_value = RateAndDividend(
                rate=rate, dividend_yield=0.0,
                source_rate="FRED", source_dividend="Polygon TTM",
            )
            row = record_iv_snapshot(
                ticker="SPY", slot="09:35", store=store, polygon=polygon, asof=asof,
            )

        assert row.error is None
        assert row.spot == pytest.approx(spot)
        assert row.iv30_vix_style is not None
        assert abs(row.iv30_vix_style - sigma) < 0.01
        prov = row.iv_provenance
        assert prov["iv_source"] == "internal_solver"
        assert prov["variance_contribution_synthetic"] == pytest.approx(0.0, abs=1e-12)
        assert prov["price_source_mix"]["opra_mid"] == pytest.approx(1.0)
        assert len(row.raw_chain) > 0
        for r in row.raw_chain:
            assert "bid" in r and "ask" in r
            assert "polygon_iv_diagnostic" in r
        # This chain carries no quote time, so the row's time is the asking time less the vendor's delay, and says so.
        asked = int(asof.timestamp() * 1000)
        assert (row.asked_at_ms, row.snapshot_ts_ms, row.quote_ts_derived) == (asked, asked - OPTIONS_SNAPSHOT_DELAY_MS, True)
        # Health score is computed on the same chain and persisted. The
        # parity test in test_iv30_stability.py validates the math; here
        # we just confirm the field is populated and in range. (Score
        # quality depends on chain density / spacing — this synthetic
        # chain has 5-dollar strikes, so half-resolution resampling moves
        # IV non-trivially and drives the composite below 0.9.)
        assert row.health_score is not None
        assert 0.0 <= row.health_score <= 1.0

    def test_a_failed_dividend_lookup_keeps_the_real_rate(self):
        """IV30 is solved and stored at the FRED rate, not at r=0, when only q is missing (#2764)."""
        store = InMemoryIvSnapshotStore()
        polygon = MagicMock()
        rate = 0.045
        asof = datetime(2026, 4, 28, 13, 35, tzinfo=UTC)
        polygon.list_snapshot_options_chain.return_value = bs_chain_payload(
            spot=591.0, sigma=0.20, rate=rate, asof=asof,
            expiry_days=[21, 28, 35, 42],
            strikes=[float(k) for k in range(540, 651, 5)],
        )

        with (
            patch("app.services.rate_dividend_service.get_risk_free_rate_and_source", return_value=(rate, "FRED")),
            patch("app.services.rate_dividend_service.compute_dividend_yield", side_effect=RuntimeError("timeout")),
        ):
            row = record_iv_snapshot(ticker="SPY", slot="09:35", store=store, polygon=polygon, asof=asof)

        assert row.error is None
        assert (row.rate, row.rate_source) == (rate, "FRED")
        assert (row.dividend_yield, row.dividend_source) == (0.0, "unknown")

    def test_a_row_is_filed_at_the_chains_latest_quote_time_not_when_the_vendor_was_asked(self):
        """Asked at 16:10 on the delayed plan, the quotes are those of 15:54:58.5; the row sits there (#3525)."""
        from app.services.rate_dividend_service import RateAndDividend

        asked = datetime(2024, 3, 11, 20, 10, tzinfo=UTC)  # 16:10 EDT
        newest = datetime(2024, 3, 11, 19, 54, 58, 500_000, tzinfo=UTC)  # 15:54:58.5 EDT
        payload = bs_chain_payload(
            spot=591.0, sigma=0.20, rate=0.045, asof=asked, expiry_days=[21, 28, 35, 42],
            strikes=[float(k) for k in range(540, 651, 5)], quote_ns=int(newest.timestamp() * 1_000_000_000) - 7_000_000_000,
        )
        payload["contracts"][17]["last_quote"]["last_updated"] = int(newest.timestamp() * 1_000_000_000)
        store, polygon = InMemoryIvSnapshotStore(), MagicMock()
        polygon.list_snapshot_options_chain.return_value = payload

        with patch(
            "app.services.iv_recorder.get_rate_and_dividend",
            return_value=RateAndDividend(rate=0.045, dividend_yield=0.0, source_rate="FRED", source_dividend="Polygon TTM"),
        ):
            row = record_iv_snapshot(ticker="SPY", slot="15:55", store=store, polygon=polygon, asof=asked)

        assert row.error is None
        assert row.snapshot_ts_ms == int(newest.timestamp() * 1000) and row.quote_ts_derived is False
        assert row.asked_at_ms == int(asked.timestamp() * 1000), "the asking time is kept beside the quote time"
        assert store.read_series("SPY", start_ms=row.snapshot_ts_ms, end_ms=row.snapshot_ts_ms) == [row]
        assert store.read_series("SPY", start_ms=row.asked_at_ms) == [], "the series is read by the quote time"

    @pytest.mark.parametrize(
        ("stamps", "expected_ms"),
        [
            pytest.param([1_500_000_000_000_000_001], 1_500_000_000_000, id="below_the_half_millisecond_rounds_down"),
            pytest.param([1_500_000_000_000_500_000], 1_500_000_000_001, id="the_half_millisecond_rounds_up"),
            pytest.param([1_500_000_000_000_499_999], 1_500_000_000_000, id="just_under_the_half_rounds_down"),
            pytest.param([1_000_000_000_000_000_000, 1_400_000_000_000_000_000, 1_200_000_000_000_000_000], 1_400_000_000_000, id="the_latest_of_many"),
            pytest.param([1_400_000_000_000_000_000, None, "x", 1.5e18, True, 0, -5], 1_400_000_000_000, id="unusable_stamps_are_not_quotes"),
            pytest.param([1_400_000_000_000_000_000, 1_900_000_000_000_000_000], 1_400_000_000_000, id="a_stamp_after_the_asking_time_is_not_a_quote_the_vendor_had"),
            pytest.param([None, "x", 1.5e18, True, 0, -5], None, id="no_usable_stamp"),
            pytest.param([], None, id="no_contract"),
        ],
    )
    def test_the_chains_latest_quote_time_is_ingested_to_the_closest_millisecond(self, stamps, expected_ms):
        contracts = [{"last_quote": {"bid": 1.0, "ask": 1.1, "last_updated": stamp}} for stamp in stamps]
        contracts += [{"last_quote": None}, {"last_quote": "n/a"}, {}]

        assert latest_quote_ms(contracts, asked_ms=1_600_000_000_000) == expected_ms

    @pytest.mark.parametrize(
        ("payload", "error_prefix"),
        [
            pytest.param(None, "polygon_fetch_failed", id="the_vendor_failed"),
            pytest.param({"underlying": {"price": 0.0}, "contracts": []}, "insufficient_snapshot", id="no_chain"),
        ],
    )
    def test_a_row_that_never_saw_a_chain_is_filed_at_the_derived_time_and_says_so(self, payload, error_prefix):
        store, polygon = InMemoryIvSnapshotStore(), MagicMock()
        if payload is None:
            polygon.list_snapshot_options_chain.side_effect = RuntimeError("502")
        else:
            polygon.list_snapshot_options_chain.return_value = payload
        asked = datetime(2024, 3, 11, 20, 10, tzinfo=UTC)

        row = record_iv_snapshot(ticker="SPY", slot="15:55", store=store, polygon=polygon, asof=asked)

        assert row.error is not None and row.error.startswith(error_prefix)
        asked_ms = int(asked.timestamp() * 1000)
        assert (row.asked_at_ms, row.snapshot_ts_ms, row.quote_ts_derived) == (asked_ms, asked_ms - OPTIONS_SNAPSHOT_DELAY_MS, True)

    def test_polygon_failure_persists_error_row(self):
        store = InMemoryIvSnapshotStore()
        polygon = MagicMock()
        polygon.list_snapshot_options_chain.side_effect = RuntimeError("polygon timeout")

        row = record_iv_snapshot(
            ticker="SPY", slot="12:30", store=store, polygon=polygon,
            asof=datetime(2026, 4, 28, 16, 30, tzinfo=UTC),
        )
        assert row.error is not None
        assert "polygon" in row.error.lower()
        # Error rows have no health score — there's no chain to score.
        assert row.health_score is None
        rows = store.read_series("SPY")
        assert len(rows) == 1
        assert rows[0].error is not None
        assert rows[0].health_score is None

    def test_records_a_real_row_from_a_vendor_that_filters_expiries(self):
        """The client's own expiry default ran against a mocked client method, so a live slot was always an error row (#3221)."""
        from app.services.polygon_client import PolygonClientService
        from app.services.rate_dividend_service import RateAndDividend
        from tests._helpers.polygon_snapshot_sdk import VendorFilteringSnapshotSdk

        store = InMemoryIvSnapshotStore()
        asof = datetime(2026, 4, 28, 13, 35, tzinfo=UTC)
        payload = bs_chain_payload(
            spot=591.0, sigma=0.20, rate=0.045, asof=asof,
            expiry_days=[21, 28, 35, 42, 120],
            strikes=[float(k) for k in range(540, 651, 5)],
        )
        polygon = PolygonClientService()
        polygon.client = VendorFilteringSnapshotSdk(payload["contracts"], spot=591.0)

        with patch(
            "app.services.iv_recorder.get_rate_and_dividend",
            return_value=RateAndDividend(
                rate=0.045, dividend_yield=0.0, source_rate="FRED", source_dividend="Polygon TTM"
            ),
        ):
            row = record_iv_snapshot(ticker="SPY", slot="09:35", store=store, polygon=polygon, asof=asof)

        assert row.error is None
        assert row.iv30_vix_style is not None
        assert abs(row.iv30_vix_style - 0.20) < 0.01
        assert store.read_series("SPY") == [row]

    @pytest.mark.parametrize(
        ("payload_for", "slot", "asof", "error_prefix", "quotes_kept"),
        [
            pytest.param(
                lambda _asof: {"underlying": {"price": 0.0}, "contracts": []},
                "16:00", datetime(2026, 4, 28, 20, 0, tzinfo=UTC), "insufficient_snapshot", False,
                id="a_snapshot_with_no_spot_persists_an_insufficient_snapshot_row",
            ),
            pytest.param(
                lambda asof: bs_chain_payload(
                    spot=591.0, sigma=0.20, rate=0.045, asof=asof,
                    expiry_days=[7, 14],  # both inside 30 days: nothing above the target
                    strikes=[float(k) for k in range(540, 651, 5)],
                ),
                "09:35", datetime(2026, 4, 28, 13, 35, tzinfo=UTC), "replication_failed", True,
                id="a_chain_that_cannot_straddle_the_target_persists_a_replication_failed_row",
            ),
            # The grouping of quotes used to sit outside the try: a 500 and no row (#3221).
            pytest.param(
                lambda _asof: {
                    "underlying": {"ticker": "SPY", "price": 591.0},
                    "contracts": [
                        {
                            "ticker": "O:SPY260520C00590000",
                            "contract_type": "call",
                            "strike_price": 590.0,
                            "expiration_ms": option_expiry_anchor_ms(date(2026, 5, 20)),
                            "last_quote": "n/a",
                        }
                    ],
                },
                "09:35", datetime(2026, 4, 28, 13, 35, tzinfo=UTC), "replication_failed", True,
                id="3221-a_quote_that_is_not_a_quote_still_writes_the_audit_row",
            ),
        ],
    )
    def test_a_snapshot_that_cannot_be_turned_into_an_iv30_still_persists_its_error_row(
        self, payload_for: Callable[[datetime], dict], slot: str, asof: datetime, error_prefix: str,
        quotes_kept: bool,
    ):
        from app.services.rate_dividend_service import RateAndDividend

        store = InMemoryIvSnapshotStore()
        polygon = MagicMock()
        polygon.list_snapshot_options_chain.return_value = payload_for(asof)

        with patch(
            "app.services.iv_recorder.get_rate_and_dividend",
            return_value=RateAndDividend(
                rate=0.045, dividend_yield=0.0, source_rate="FRED", source_dividend="Polygon TTM"
            ),
        ):
            row = record_iv_snapshot(ticker="SPY", slot=slot, store=store, polygon=polygon, asof=asof)

        assert row.error is not None and row.error.startswith(error_prefix), "the row names why it failed"
        assert row.iv30_vix_style is None, "a failed snapshot has no IV30"
        assert (len(row.raw_chain) > 0) is quotes_kept, "the quotes are kept so the slot can be re-derived"
        assert store.read_series("SPY") == [row], "the error row is the one row persisted"

    def test_the_dominance_gate_event_carries_only_what_the_replicator_reports(self, caplog):
        import logging

        from app.services.rate_dividend_service import RateAndDividend
        from app.volatility.iv_provenance import IvProvenance

        store = InMemoryIvSnapshotStore()
        polygon = MagicMock()
        asof = datetime(2026, 4, 28, 13, 35, tzinfo=UTC)
        polygon.list_snapshot_options_chain.return_value = bs_chain_payload(
            spot=591.0, sigma=0.20, rate=0.045, asof=asof,
            expiry_days=[21, 28, 35, 42],
            strikes=[float(k) for k in range(540, 651, 5)],
        )
        gated = IvProvenance(
            iv_source="internal_solver",
            price_source_mix={"opra_mid": 1.0},
            variance_contribution_synthetic=0.0,
            strike_coverage_score=0.9,
            max_single_strike_share=0.41,
            single_strike_dropped=2,
            single_strike_hard_failed=False,
        )

        with (
            patch(
                "app.services.iv_recorder.get_rate_and_dividend",
                return_value=RateAndDividend(
                    rate=0.045, dividend_yield=0.0, source_rate="FRED", source_dividend="Polygon TTM"
                ),
            ),
            patch("app.services.iv_recorder.vix_style_iv30_with_provenance", return_value=(0.20, gated)),
            caplog.at_level(logging.WARNING),
        ):
            record_iv_snapshot(ticker="SPY", slot="09:35", store=store, polygon=polygon, asof=asof)

        (event,) = [r for r in caplog.records if getattr(r, "event", None) == "iv_dominance_gate"]
        assert (event.max_share_after, event.iterations, event.hard_failed) == (0.41, 2, False)
        # The replicator reports no pre-drop share and no strike count, so the event has none.
        assert not hasattr(event, "max_share_before")
        assert not hasattr(event, "strikes_remaining")


class TestDefaultStorePath:
    def test_the_default_store_writes_under_the_configured_directory(self, tmp_path: Path):
        """``IV_RECORDER_STORE_PATH`` is read when the process starts, so this runs one."""
        import os
        import subprocess
        import sys

        code = (
            "from app.services.iv_recorder import RecordedIvSnapshot, get_iv_store\n"
            "get_iv_store().write(RecordedIvSnapshot(ticker='SPY', snapshot_ts_ms=1, slot='16:00', spot=0.0, "
            "rate=0.0, dividend_yield=0.0, rate_source='x', dividend_source='x', iv30_vix_style=None, "
            "iv30_parametric=None, iv_provenance={}, raw_chain=[]))\n"
        )
        env = {**os.environ, "IV_RECORDER_STORE_PATH": str(tmp_path), "POLYGON_API_KEY": "test"}

        subprocess.run(
            [sys.executable, "-W", "ignore", "-c", code],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            check=True,
            capture_output=True,
            timeout=180,
        )

        assert (tmp_path / "SPY.jsonl").exists()


@pytest.fixture
def in_memory_store():
    """Replace the module-level store with an in-memory one for the test."""
    from app.services.iv_recorder import get_iv_store, set_iv_store

    new_store = InMemoryIvSnapshotStore()
    original = get_iv_store()
    set_iv_store(new_store)
    try:
        yield new_store
    finally:
        set_iv_store(original)


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


class TestRecorderRoutes:
    async def test_snapshot_route_invalid_slot_returns_400(self, client):
        resp = await client.post(
            "/api/iv-recorder/snapshot",
            json={"ticker": "SPY", "slot": "10:00", "target_calendar_days": 30},
        )
        assert resp.status_code == 400

    async def test_snapshot_route_files_a_lower_case_ticker_under_the_upper_case_one(self, client, in_memory_store):
        from app.routers import iv_recorder as iv_recorder_router

        with patch.object(
            iv_recorder_router.polygon_client, "list_snapshot_options_chain",
            return_value={"underlying": {"ticker": "SPY", "price": 0.0}, "contracts": []},
        ):
            resp = await client.post("/api/iv-recorder/snapshot", json={"ticker": " spy", "slot": "09:35"})

        assert resp.status_code == 200, resp.text
        assert resp.json()["snapshot"]["ticker"] == "SPY"
        assert len(in_memory_store.read_series("SPY")) == 1

    @pytest.mark.parametrize("ticker", ["../SPY", "SPY/QQQ", "A" * 13])
    async def test_snapshot_route_refuses_a_ticker_that_is_not_one(self, client, in_memory_store, ticker):
        resp = await client.post("/api/iv-recorder/snapshot", json={"ticker": ticker, "slot": "09:35"})

        assert resp.status_code == 422
        assert in_memory_store.read_series(ticker) == []

    async def test_snapshot_route_records_a_slot(self, client, in_memory_store):
        from app.routers import iv_recorder as iv_recorder_router

        spot = 591.0
        sigma = 0.20
        rate = 0.045
        asof = datetime.now(tz=UTC)
        polygon_payload = bs_chain_payload(
            spot=spot, sigma=sigma, rate=rate, asof=asof,
            expiry_days=[21, 28, 35, 42],
            strikes=[float(k) for k in range(540, 651, 5)],
        )
        with (
            patch.object(iv_recorder_router.polygon_client, "list_snapshot_options_chain",
                         return_value=polygon_payload),
            patch("app.services.iv_recorder.get_rate_and_dividend") as rd,
        ):
            from app.services.rate_dividend_service import RateAndDividend
            rd.return_value = RateAndDividend(
                rate=rate, dividend_yield=0.0,
                source_rate="FRED", source_dividend="Polygon TTM",
            )
            resp = await client.post(
                "/api/iv-recorder/snapshot",
                json={"ticker": "SPY", "slot": "09:35"},
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["success"] is True
            assert body["snapshot"]["iv30_vix_style"] is not None
            assert body["snapshot"]["error"] is None
