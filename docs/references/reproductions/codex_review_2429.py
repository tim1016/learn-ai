"""Guarded synthetic publication/read interleavings for review #2429.

Tests characterize the pinned baseline. The fake connection models only
transaction commit/rollback state; no Postgres or real launcher is used.
Production publication, file readers, and sidecar orchestration execute.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import PurePosixPath
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.atomic import publish_artifact
from app.data_lake.lean_writer import build_minute_trade_zip_bytes
from app.data_lake.path_policy import lake_subpath
from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.data.policy_store import snapshot_minute_trade_zips
from app.lean_sidecar.data_policy import BarsSpec, DataPolicy
from app.lean_sidecar.lake_mount import resolve_lake_artifacts
from app.lean_sidecar.launcher.models import LAUNCHER_CAPABILITIES, LaunchResponse
from app.lean_sidecar.trading_calendar import next_trading_day, session_open_ms_utc
from app.research.sweep import snapshot
from app.schemas.engine_backtest import EngineBacktestRequest
from app.services import engine_backtest_service as engine_service
from app.services import lean_sidecar_service as lean_service
from tests._helpers.lake_fixture import seed_lake_corporate_actions, seed_lake_window, to_lake_bars
from tests._helpers.lean_store import make_minute_bars


SYMBOL = "SPY"
DAY = date(2026, 1, 5)
RELATIVE = PurePosixPath("equity/usa/minute/spy/20260105_trade.zip")


class FakeConnection:
    """Only the DB boundary is simulated; production SQL call ordering runs."""

    def __init__(self, old_hash: str, failure: str | None = None):
        self.row = {
            "Status": "fetching", "LeaseOwner": "review-2429", "LeaseGeneration": 2,
            "LeaseExpiresAtMs": 9_000_000_000_000, "FileSha256": old_hash,
        }
        self.failure = failure

    @asynccontextmanager
    async def transaction(self):
        before = dict(self.row)
        try:
            yield
            if self.failure == "commit":
                raise RuntimeError("synthetic commit failure after rename")
        except BaseException:
            self.row = before
            raise

    async def fetchrow(self, query, *args):
        assert "FOR UPDATE" in query
        return self.row

    async def execute(self, query, *args):
        assert query == catalog_client._COMPLETE_ARTIFACT_SQL
        if self.failure == "execute":
            raise RuntimeError("synthetic completion failure after rename")
        self.row["Status"] = "complete"
        self.row["FileSha256"] = args[5]
        return "UPDATE 1"


async def _publish(root, relative, payload, connection):
    staging = root.parent / "synthetic-staging"
    staging.mkdir(exist_ok=True)

    @asynccontextmanager
    async def connect():
        yield connection

    with patch.object(catalog_client, "connection", connect):
        return await publish_artifact(
            content=payload, lake_root=root, staging_root=staging,
            rel_lake_path=relative, request_id=uuid4(), worker_id="review-2429",
            attempt=1, artifact_id=2429, lease_generation=2, row_count=390,
            first_bar_start_ms=session_open_ms_utc(DAY),
            last_bar_start_ms=session_open_ms_utc(DAY) + 389 * 60_000,
        )


def _changed_payload():
    bars = to_lake_bars(make_minute_bars(SYMBOL, DAY))
    # A valid provider correction, unrelated to corporate-action semantics.
    from dataclasses import replace
    bars = [replace(b, open=Decimal(777), high=Decimal(777), low=Decimal(777), close=Decimal(777)) for b in bars]
    return build_minute_trade_zip_bytes(SYMBOL, DAY.strftime("%Y%m%d"), bars)


def _policy():
    return DataPolicy(
        source="polygon", symbol=SYMBOL, adjusted=False, session="regular",
        input_bars=BarsSpec(timespan="minute", multiplier=1),
        strategy_bars=BarsSpec(timespan="minute", multiplier=15),
        timestamp_policy="bar_close_ms_utc", timezone="America/New_York",
        provider_kind="live", fixture_id=None, fixture_sha256=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["minute", "factor"])
async def test_lean_manifest_records_replacement_after_simulated_reader_consumed_prior_bytes(tmp_path, monkeypatch, kind):
    writer = tmp_path / "writer"
    root = writer / lake_subpath("raw")
    seed_lake_window(root, SYMBOL, [DAY])
    factor, _ = seed_lake_corporate_actions(root, SYMBOL, factor_rows="20260105,1,1,100\n")
    relative = RELATIVE if kind == "minute" else PurePosixPath(factor.relative_to(root).as_posix())
    path = root / relative
    old_payload = path.read_bytes()
    new_payload = _changed_payload() if kind == "minute" else b"20260105,0.5,1,100\n"
    conn = FakeConnection(sha256(old_payload).hexdigest())
    consumed = []

    async def fake_launch(request):
        assert request.mount_lake_read_only
        consumed.append(path.read_bytes())
        # Deterministic interleaving: simulated engine has read A; a fully
        # authorized real publisher replaces it with B before launch returns.
        await _publish(root, relative, new_payload, conn)
        return LaunchResponse(run_id=request.run_id, exit_code=1, duration_ms=5,
                              timed_out=False, log_tail="offline simulated consumer",
                              lean_errors={}, is_clean=False)

    from app.lean_sidecar import launcher_client
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(writer))
    monkeypatch.setattr(lean_service, "DEFAULT_ARTIFACTS_ROOT", tmp_path / "artifacts")
    monkeypatch.setattr(lean_service, "assert_lean_persistence_source_current", lambda: None)
    monkeypatch.setattr(lean_service, "post_launch", fake_launch)
    monkeypatch.setattr(lean_service, "_persist_completed_run", AsyncMock(return_value=2429))
    monkeypatch.setattr(launcher_client, "get_healthz", AsyncMock(return_value={
        "status": "ok", "capabilities": list(LAUNCHER_CAPABILITIES),
    }))
    request = lean_service.TrustedRunRequest(
        run_id=f"review-snapshot-{kind}", start_ms_utc=session_open_ms_utc(DAY),
        end_ms_utc=session_open_ms_utc(next_trading_day(DAY)), starting_cash=100_000,
        data_policy=_policy(),
    )
    result = await lean_service.run_trusted_sample(request)
    manifest = json.loads(result.manifest_path.read_text())
    if kind == "minute":
        recorded = manifest["staged_zip_sha256"][relative.as_posix()]
    else:
        recorded = manifest["staged_data"]["factor_files"][0]["sha256"]
    assert consumed == [old_payload]
    assert recorded == sha256(new_payload).hexdigest()
    assert recorded != sha256(consumed[0]).hexdigest()
    assert conn.row["Status"] == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["execute", "commit"])
async def test_post_rename_failure_leaves_uncommitted_bytes_admissible_to_filesystem_readers(tmp_path, failure):
    root = tmp_path / "lake"
    seed_lake_window(root, SYMBOL, [DAY])
    path = root / RELATIVE
    old_hash = sha256(path.read_bytes()).hexdigest()
    conn = FakeConnection(old_hash, failure)
    with pytest.raises(RuntimeError, match="after rename"):
        await _publish(root, RELATIVE, _changed_payload(), conn)
    assert conn.row["Status"] == "fetching"
    assert conn.row["FileSha256"] == old_hash
    assert sha256(path.read_bytes()).hexdigest() != old_hash
    artifacts = resolve_lake_artifacts(lake_root=root, symbol=SYMBOL, start=DAY, end=DAY)
    assert path in artifacts.trade_zip_paths
    assert LeanMinuteDataReader(root).read_day(SYMBOL, DAY)[0].close == Decimal(777)
    # Even a NEW bound snapshot can bless these bytes, since capture does
    # not consult publication status. An OLD snapshot would refuse them.
    captured = snapshot.capture_data_snapshot(roots=[root], symbol=SYMBOL, resolution="minute", data_start=DAY, data_end=DAY)
    bound = snapshot.ManifestBoundMinuteReader(root, captured.artifacts)
    assert bound.read_day(SYMBOL, DAY)[0].close == Decimal(777)


def test_python_compatibility_pin_is_not_bound_to_later_plain_reader(tmp_path, monkeypatch):
    seed_lake_window(tmp_path, SYMBOL, [DAY])
    path = tmp_path / RELATIVE
    old_hash = sha256(path.read_bytes()).hexdigest()
    request = EngineBacktestRequest(
        strategy_name="ema_crossover_signal", params={"symbol": SYMBOL},
        from_date="2026-01-02", to_date="2026-01-06", resolution="minute",
        compatibility_profile="us-equity-raw-ibkr-v1", data_policy=asdict(_policy()),
        auto_fetch=False, save_study=False,
    )
    pinned = snapshot_minute_trade_zips([tmp_path], symbol=SYMBOL, start=DAY, end=DAY, adjusted=False, session="regular")["fixture_sha256"]
    monkeypatch.setattr(engine_service, "_resolve_lean_data_roots", lambda **kwargs: [tmp_path])

    def on_phase(phase):
        if phase == "consolidating_bars":
            assert request.data_policy.fixture_sha256 == pinned
            asyncio.run(_publish(tmp_path, RELATIVE, _changed_payload(), FakeConnection(old_hash)))

    response = engine_service.execute_engine_backtest(request=request, on_phase=on_phase, on_log=lambda message: None)
    assert response.success
    assert response.chart_bars
    assert all(bar["c"] == 777 for bar in response.chart_bars)
    assert response.data_policy.fixture_sha256 == pinned
    fresh = snapshot_minute_trade_zips([tmp_path], symbol=SYMBOL, start=DAY, end=DAY, adjusted=False, session="regular")
    assert request.data_policy.fixture_sha256 == pinned
    assert fresh["fixture_sha256"] != pinned


def test_bound_reader_parses_verified_payload_even_if_path_replaced_after_check(tmp_path, monkeypatch):
    seed_lake_window(tmp_path, SYMBOL, [DAY])
    path = tmp_path / RELATIVE
    old_payload = path.read_bytes()
    captured = snapshot.capture_data_snapshot(roots=[tmp_path], symbol=SYMBOL, resolution="minute", data_start=DAY, data_end=DAY)
    real_verify = snapshot._verify_bytes

    def replace_after_verification(manifest, relative, payload):
        real_verify(manifest, relative, payload)
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(_changed_payload())
        replacement.replace(path)

    monkeypatch.setattr(snapshot, "_verify_bytes", replace_after_verification)
    bound = snapshot.ManifestBoundMinuteReader(tmp_path, captured.artifacts)
    actual = bound.read_day(SYMBOL, DAY)
    expected = LeanMinuteDataReader(tmp_path).parse_day_zip(old_payload, SYMBOL, DAY)
    assert actual == expected
    with pytest.raises(snapshot.DataSnapshotMismatchError):
        bound.read_day(SYMBOL, DAY)
