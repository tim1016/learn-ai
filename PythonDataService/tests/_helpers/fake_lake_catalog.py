"""In-memory data-lake catalog and LEAN-launcher stand-ins for lake tests.

Shared by every test that drives the real ``ensure_data`` pipeline without
Postgres: the catalog is replaced by :class:`FakeCatalog`, which reproduces
the two properties ``ensure_data`` depends on -- a claim is a unique-key
insert exactly one caller wins, and a row is selectable only once it has
been completed -- and the launcher's metadata extraction is mocked at the
HTTP layer with respx. Moved here from
``tests/unit/data_lake/test_run_materialization.py`` so the PR-gated
consumer tests (the return study reading what a capture wrote, #2452) can
drive the same pipeline the daily-only materialization suite does.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest
import respx

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.types import ArtifactRecord
from app.lean_sidecar import config as sidecar_config


class FakeCatalog:
    """Stand-in for the Postgres catalog, faithful to its claim semantics.

    Each ``claim_*`` mirrors an ``INSERT ... ON CONFLICT DO NOTHING`` against
    the partial unique index for that artifact kind: the first caller for a
    key gets an id, every later caller gets ``None`` until the row is gone.
    Each ``select_complete_*`` mirrors the ``Status = 'complete'`` predicate,
    so a claimed-but-unfinished artifact is invisible to the loser of the
    race — which is what makes contention observable rather than papered
    over. The methods contain no ``await``, so on one event loop a claim is
    as indivisible as the SQL statement it stands in for.
    """

    def __init__(self) -> None:
        self.rows: dict[int, dict] = {}
        self.keys: dict[tuple, int] = {}
        self._next_id = 1

    # -- claim helpers ------------------------------------------------------

    def _claim(self, key: tuple, row: dict) -> int | None:
        if key in self.keys:
            return None
        artifact_id = self._next_id
        self._next_id += 1
        self.keys[key] = artifact_id
        self.rows[artifact_id] = {
            **row,
            "id": artifact_id,
            "status": "fetching",
            "attempt_count": 1,
            "last_error": None,
            # Fencing generation (issue #1888): mirrors the real schema's
            # LeaseGeneration column, starting at
            # catalog_client.INITIAL_LEASE_GENERATION on every fresh claim.
            "lease_generation": catalog_client.INITIAL_LEASE_GENERATION,
        }
        return artifact_id

    @staticmethod
    def _identity_row(identity, data_contract_hash: str, file_path: str) -> dict:
        return {
            "artifact_kind": identity.artifact_kind,
            "market": identity.market,
            "symbol": identity.symbol,
            "trading_date": identity.trading_date,
            "resolution": identity.resolution,
            "data_type": identity.data_type,
            "provider": identity.provider,
            "price_adjustment_mode": identity.price_adjustment_mode,
            "data_contract_hash": data_contract_hash,
            "file_path": file_path,
            "file_sha256": "",
            "row_count": None,
            "first_bar_start_ms": None,
            "last_bar_start_ms": None,
        }

    _BOOKKEEPING_KEYS = frozenset({"status", "attempt_count", "last_error", "lease_generation"})

    @classmethod
    def _record(cls, row: dict) -> ArtifactRecord:
        return ArtifactRecord(**{k: v for k, v in row.items() if k not in cls._BOOKKEEPING_KEYS})

    # -- catalog_client surface --------------------------------------------

    async def init_pool(self) -> None:
        return None

    async def claim_metadata_artifact(
        self, identity, worker_id, lease_ttl_ms, data_contract_hash, file_path
    ) -> int | None:
        return self._claim(
            ("metadata", data_contract_hash),
            self._identity_row(identity, data_contract_hash, file_path),
        )

    async def select_complete_metadata_artifact(
        self, data_contract_hash: str, data_root_id=None
    ) -> ArtifactRecord | None:
        # data_root_id accepted-not-modeled: this fake has never partitioned
        # any of its tables by root (see select_coverage_minute_bars above),
        # so a caller passing the real function's now-optional data_root_id
        # kwarg (#1879, PR C of #1861) must not TypeError -- the same reason
        # every other newly-optional kwarg on this fake's methods is accepted.
        for row in self.rows.values():
            if (
                row["artifact_kind"] == "metadata"
                and row["data_contract_hash"] == data_contract_hash
                and row["status"] == "complete"
            ):
                return self._record(row)
        return None

    async def select_metadata_claim_state(
        self, data_contract_hash: str, data_root_id=None
    ) -> catalog_client.ArtifactClaimState | None:
        artifact_id = self.keys.get(("metadata", data_contract_hash))
        if artifact_id is None:
            return None
        row = self.rows[artifact_id]
        return catalog_client.ArtifactClaimState(
            id=artifact_id,
            status=row["status"],
            attempt_count=row["attempt_count"],
            last_error=row["last_error"],
        )

    async def mark_metadata_artifacts_stale_for_path(
        self, data_root_id, price_adjustment_mode, file_path, keep_artifact_id=None
    ) -> int:
        """Mirrors the real query's mode-scoped staleness predicate (#1879):
        FilePath alone is identical across modes, so PriceAdjustmentMode
        must gate which rows this call is allowed to touch -- see
        ``catalog_client.mark_metadata_artifacts_stale_for_path``'s own
        docstring for why. Also mirrors that function's two Codex-P1/P2
        fixes (PR #1884): a legacy pre-#1879 row's ``price_adjustment_mode``
        is ``None`` and must still match any requested mode, and
        ``keep_artifact_id`` omitted (``None``) means "no keeper, stale
        every complete row for this path" rather than excluding nothing."""
        staled = 0
        for artifact_id, row in self.rows.items():
            if (
                row["artifact_kind"] == "metadata"
                and (row["price_adjustment_mode"] == price_adjustment_mode or row["price_adjustment_mode"] is None)
                and row["file_path"] == file_path
                and row["status"] == "complete"
                and (keep_artifact_id is None or artifact_id != keep_artifact_id)
            ):
                row["status"] = "stale"
                staled += 1
        return staled

    @staticmethod
    def _minute_key(identity) -> tuple:
        return (
            "minute",
            identity.market,
            identity.symbol,
            identity.trading_date,
            identity.data_type,
            identity.provider,
            identity.price_adjustment_mode,
        )

    async def claim_minute_bar(self, identity, worker_id, lease_ttl_ms, data_contract_hash, file_path) -> int | None:
        return self._claim(self._minute_key(identity), self._identity_row(identity, data_contract_hash, file_path))

    async def select_minute_bar_claim_state(self, identity) -> catalog_client.ArtifactClaimState | None:
        artifact_id = self.keys.get(self._minute_key(identity))
        if artifact_id is None:
            return None
        row = self.rows[artifact_id]
        return catalog_client.ArtifactClaimState(
            id=artifact_id,
            status=row["status"],
            attempt_count=row["attempt_count"],
            last_error=row["last_error"],
        )

    async def steal_or_retry_minute_bar(
        self, artifact_id, worker_id, lease_ttl_ms, max_retries, *, bypass_retry_ceiling: bool = False
    ) -> int | None:
        row = self.rows[artifact_id]
        # The fake has no lease clock, so "fetching" always means a live
        # lease held by someone else — nothing to steal, matching the real
        # WHERE clause's "LeaseExpiresAtMs < now" arm never firing here.
        # 'stale' reactivates unconditionally (Codex P1, PR #1884) -- see
        # the real ``steal_or_retry_minute_bar``'s docstring for why that
        # branch carries no lease/retry gate, unlike the other two.
        # ``bypass_retry_ceiling`` (#1889) is modelled rather than ignored:
        # a fake that accepted the flag and dropped it would answer "no
        # eligible row" for exactly the launcher-outage case the flag exists
        # to keep retryable, which is the bug it would be there to catch.
        retryable = row["attempt_count"] < max_retries or bypass_retry_ceiling
        if (row["status"] == "failed" and retryable) or row["status"] == "stale":
            row["status"] = "fetching"
            row["attempt_count"] += 1
            row["last_error"] = None
            row["lease_generation"] += 1
            return row["lease_generation"]
        return None

    async def select_coverage_minute_bars(
        self, market, symbol, data_type, start_trading_date, end_trading_date, *, price_adjustment_mode
    ) -> list[ArtifactRecord]:
        # Mode is a required filter in the real query (#1832): two modes can
        # coexist for one (market, symbol, date, data_type), and a fake that
        # ignored the distinction would hide exactly the wrong-row bug the
        # required parameter exists to prevent.
        #
        # Both bounds None means unbounded — the daily-trade rollup's
        # symbol-wide source read (#1870) — mirroring the real query's
        # null-safe BETWEEN predicate.
        def _in_window(trading_date) -> bool:
            if start_trading_date is None and end_trading_date is None:
                return True
            return start_trading_date <= trading_date <= end_trading_date

        return [
            self._record(row)
            for row in self.rows.values()
            if row["artifact_kind"] == "time_series_bars"
            and row["resolution"] == "minute"
            and row["market"] == market
            and row["symbol"] == symbol
            and row["data_type"] == data_type
            and row["price_adjustment_mode"] == price_adjustment_mode
            and row["status"] == "complete"
            and _in_window(row["trading_date"])
        ]

    async def claim_aggregated_bar_artifact(
        self, identity, worker_id, lease_ttl_ms, data_contract_hash, file_path
    ) -> int | None:
        key = (
            "aggregated",
            identity.market,
            identity.symbol,
            identity.resolution,
            identity.data_type,
            identity.provider,
            identity.price_adjustment_mode,
        )
        return self._claim(key, self._identity_row(identity, data_contract_hash, file_path))

    async def select_complete_aggregated_bar_artifact(self, identity) -> ArtifactRecord | None:
        for row in self.rows.values():
            if (
                row["artifact_kind"] == "time_series_bars"
                and row["resolution"] == identity.resolution
                and row["market"] == identity.market
                and row["symbol"] == identity.symbol
                and row["data_type"] == identity.data_type
                and row["status"] == "complete"
            ):
                return self._record(row)
        return None

    @staticmethod
    def _corp_action_key(identity) -> tuple:
        # The real partial unique index uq_data_lake_artifacts_corp_actions:
        # one factor_file / map_file row per (market, symbol, kind, provider,
        # mode) -- never per window, which is why a rebuild is a refresh of
        # that one row rather than a second claim.
        return (
            "corp_action",
            identity.market,
            identity.symbol,
            identity.artifact_kind,
            identity.provider,
            identity.price_adjustment_mode,
        )

    async def claim_corp_action_artifact(
        self, identity, worker_id, lease_ttl_ms, data_contract_hash, file_path
    ) -> int | None:
        return self._claim(self._corp_action_key(identity), self._identity_row(identity, data_contract_hash, file_path))

    async def select_complete_corp_action_artifact(self, identity) -> ArtifactRecord | None:
        artifact_id = self.keys.get(self._corp_action_key(identity))
        if artifact_id is None:
            return None
        row = self.rows[artifact_id]
        return self._record(row) if row["status"] == "complete" else None

    async def select_corp_action_claim_state(self, identity) -> catalog_client.ArtifactClaimState | None:
        artifact_id = self.keys.get(self._corp_action_key(identity))
        if artifact_id is None:
            return None
        row = self.rows[artifact_id]
        return catalog_client.ArtifactClaimState(
            id=artifact_id,
            status=row["status"],
            attempt_count=row["attempt_count"],
            last_error=row["last_error"],
        )

    async def refresh_complete_artifact(self, artifact_id, worker_id, lease_ttl_ms) -> catalog_client.PriorArtifactMetadata | None:
        row = self.rows.get(artifact_id)
        if row is None or row["status"] != "complete":
            return None
        row["lease_generation"] += 1
        prior = catalog_client.PriorArtifactMetadata(
            prior_file_path=row["file_path"],
            prior_file_sha256=row["file_sha256"],
            new_lease_generation=row["lease_generation"],
        )
        row.update(status="fetching")
        return prior

    async def restore_complete_artifact(self, artifact_id, worker_id, lease_generation) -> bool:
        row = self.rows.get(artifact_id)
        # Owner AND generation, like the real guard: a stale caller sharing
        # the per-process worker id must not restore a newer generation.
        if row is None or row["status"] != "fetching" or row["lease_generation"] != lease_generation:
            return False
        row.update(status="complete")
        return True

    async def complete_artifact(
        self,
        artifact_id,
        row_count,
        first_bar_start_ms,
        last_bar_start_ms,
        file_size_bytes,
        file_sha256,
        lease_generation,
        data_contract_hash=None,
    ) -> bool:
        row = self.rows[artifact_id]
        # Status AND generation (issue #1888) -- mirrors the real guard's
        # tightening from status-only. Returns whether the row was actually
        # completed: a caller that treated a refused completion as success
        # would report an ArtifactRecord describing someone else's row.
        if row["status"] != "fetching" or row["lease_generation"] != lease_generation:
            return False
        row.update(
            status="complete",
            row_count=row_count,
            first_bar_start_ms=first_bar_start_ms,
            last_bar_start_ms=last_bar_start_ms,
            file_sha256=file_sha256,
            data_contract_hash=data_contract_hash if data_contract_hash is not None else row["data_contract_hash"],
            last_error=None,
        )
        return True

    async def publish_under_lease(
        self,
        *,
        artifact_id,
        worker_id,
        lease_generation,
        promote,
        row_count,
        first_bar_start_ms,
        last_bar_start_ms,
        file_size_bytes,
        file_sha256,
        data_contract_hash=None,
    ) -> None:
        """Authorize, promote, then record -- in that order, and only in that
        order. The real implementation makes the three inseparable by holding
        a row lock across them; this fake keeps the same *observable*
        contract, which is what the code under test depends on: an
        unauthorized writer never sees ``promote`` called at all.
        """
        row = self.rows.get(artifact_id)
        if (
            row is None
            or row["status"] != "fetching"
            or row["lease_generation"] != lease_generation
            or row.get("lease_owner", worker_id) != worker_id
        ):
            raise catalog_client.ArtifactLeaseLostError(
                f"artifact {artifact_id}: {worker_id} is not authorized to publish at generation {lease_generation}"
            )
        promote()
        completed = await self.complete_artifact(
            artifact_id=artifact_id,
            row_count=row_count,
            first_bar_start_ms=first_bar_start_ms,
            last_bar_start_ms=last_bar_start_ms,
            file_size_bytes=file_size_bytes,
            file_sha256=file_sha256,
            lease_generation=lease_generation,
            data_contract_hash=data_contract_hash,
        )
        assert completed, "the fake authorized a publication it then refused to complete"

    async def fail_artifact(self, artifact_id, last_error, error_message=None, *, worker_id, lease_generation) -> bool:
        row = self.rows.get(artifact_id)
        if row is None or row["lease_generation"] != lease_generation:
            return False
        row.update(status="failed", last_error=last_error)
        return True

    async def mark_complete_artifact_failed(self, artifact_id, last_error, error_message=None) -> bool:
        row = self.rows.get(artifact_id)
        if row is None or row["status"] != "complete":
            return False
        row.update(status="failed", last_error=last_error)
        return True


MARKET_HOURS_JSON = json.dumps(
    {
        "entries": {
            "Equity-usa-[*]": {
                "exchange": "NYSE",
                "timezone": "America/New_York",
                "holidays": [],
                "earlyCloses": {},
            }
        }
    }
).encode("utf-8")
SYMBOL_PROPERTIES_CSV = b"SPY,equity,usd,1,0\n"


def stage_workspace_files(artifacts_root: Path, run_id: str) -> None:
    """Pre-place the two files a real launcher run would have written.

    Layout must match app.lean_sidecar.workspace.Workspace.data_dir and
    staging.list_metadata_databases: <root>/<run_id>/workspace/data/...
    """
    data_dir = artifacts_root / run_id / "workspace" / "data"
    (data_dir / "market-hours").mkdir(parents=True, exist_ok=True)
    (data_dir / "symbol-properties").mkdir(parents=True, exist_ok=True)
    (data_dir / "market-hours" / "market-hours-database.json").write_bytes(MARKET_HOURS_JSON)
    (data_dir / "symbol-properties" / "symbol-properties-database.csv").write_bytes(SYMBOL_PROPERTIES_CSV)



def launcher_responder(*, latency_s: float = 0.0):
    """Stand-in for the launcher: stages the files app.data_lake.lean_metadata
    will read back (keyed by the run_id the caller sent), then returns the
    launcher's actual paths-only response shape — see module docstring on
    stage_workspace_files for the layout contract."""

    async def _respond(request: httpx.Request) -> httpx.Response:
        if latency_s:
            await asyncio.sleep(latency_s)
        body = json.loads(request.content)
        stage_workspace_files(sidecar_config.DEFAULT_ARTIFACTS_ROOT, body["run_id"])
        return httpx.Response(
            200,
            json={
                "market_hours_db_path": "/launcher-side/market-hours-database.json",
                "symbol_properties_db_path": "/launcher-side/symbol-properties-database.csv",
            },
        )

    return _respond


def mock_launcher(*, latency_s: float = 0.0):
    return respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=launcher_responder(latency_s=latency_s)
    )


#: Every ``catalog_client`` function :class:`FakeCatalog` stands in for.
FAKE_CATALOG_FUNCTIONS: tuple[str, ...] = (
    "init_pool",
    "claim_metadata_artifact",
    "select_complete_metadata_artifact",
    "select_metadata_claim_state",
    "mark_metadata_artifacts_stale_for_path",
    "claim_minute_bar",
    "select_minute_bar_claim_state",
    "steal_or_retry_minute_bar",
    "select_coverage_minute_bars",
    "claim_aggregated_bar_artifact",
    "select_complete_aggregated_bar_artifact",
    "claim_corp_action_artifact",
    "select_complete_corp_action_artifact",
    "select_corp_action_claim_state",
    "refresh_complete_artifact",
    "restore_complete_artifact",
    "complete_artifact",
    "publish_under_lease",
    "fail_artifact",
    "mark_complete_artifact_failed",
)


def install_fake_catalog(monkeypatch: pytest.MonkeyPatch) -> FakeCatalog:
    """Replace the Postgres catalog with a fresh :class:`FakeCatalog`."""
    catalog = FakeCatalog()
    for name in FAKE_CATALOG_FUNCTIONS:
        monkeypatch.setattr(catalog_client, name, getattr(catalog, name))
    return catalog


def point_lake_writer_at_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point LEAN_DATA_WRITE_ROOT at a tmp_path tree with lake/ + staging/.

    Also points app.lean_sidecar.config.DEFAULT_ARTIFACTS_ROOT at a sibling
    tmp_path tree: Phase 0 reads the launcher's extracted metadata files back
    off that root (app.data_lake.lean_metadata does not trust the launcher's
    HTTP response body, only its own view of the shared mount), so
    :func:`mock_launcher` stages files there instead of under the real repo
    path. Returns the writer root.
    """
    write_root = tmp_path / "writer-root"
    (write_root / "lake").mkdir(parents=True)
    (write_root / "staging").mkdir(parents=True)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(settings, "POLYGON_API_KEY", "test-key")
    monkeypatch.setattr(settings, "LEAN_LAUNCHER_URL", "http://launcher-mock:8090")
    monkeypatch.setattr(settings, "LEAN_LAUNCHER_TOKEN", "test-token")
    monkeypatch.setenv("LEAN_LAUNCHER_TOKEN", "test-token")
    artifacts_root = tmp_path / "artifacts-root"
    artifacts_root.mkdir(parents=True)
    monkeypatch.setattr(sidecar_config, "DEFAULT_ARTIFACTS_ROOT", artifacts_root)
    return write_root
