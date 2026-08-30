"""Unit tests for app.data_lake.metadata_bundle (#1879, PR C of #1861).

Two tiers:

- Pure receipt tests (no Postgres, no launcher): the Pydantic models, the
  read/verify functions, and the data-contract-hash recipe.
- ``ensure_lean_metadata_bundle`` end-to-end tests: Postgres-gated (skip
  when ``POSTGRES_URL`` is unset, same pattern as
  ``test_catalog_write_ops.py``), launcher HTTP mocked with ``respx``.
  These are the acceptance-criteria tests from the live issue: tampering
  detected, a digest change re-extracts, a crash before receipt publication
  cannot produce a false cache hit, concurrent different-digest requests
  cannot interleave, old catalog rows are staled.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
import respx

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.metadata_bundle import (
    RECEIPT_SCHEMA_VERSION,
    LeanMetadataFiles,
    LeanMetadataReceipt,
    MetadataBootstrap,
    MetadataBundleError,
    MetadataFileEntry,
    ensure_lean_metadata_bundle,
    metadata_data_contract_hash,
    read_receipt,
    receipt_path,
    verify_bundle,
    verify_files_on_disk,
    verify_receipt_identity,
)
from app.data_lake.types import DataRunSpec
from app.lean_sidecar import config as sidecar_config
from tests._helpers.lake_fixture import seed_lean_metadata_receipt

_ROOT_A = UUID("11111111-1111-1111-1111-111111111111")
_ROOT_B = UUID("22222222-2222-2222-2222-222222222222")


# ---------------------------------------------------------------------------
# Pure receipt model tests -- no Postgres, no filesystem beyond tmp_path
# ---------------------------------------------------------------------------


class TestLeanMetadataFiles:
    def test_missing_interest_rate_key_fails_validation(self):
        """An incomplete/pre-#1859 receipt omits the key entirely -- must be
        untrusted (fails validation), never silently read as 'not produced'."""
        with pytest.raises(Exception, match="interest_rate"):
            LeanMetadataFiles.model_validate(
                {
                    "market_hours": {"file_path": "market-hours/market-hours-database.json", "sha256": "a" * 64},
                    "symbol_properties": {"file_path": "symbol-properties/symbol-properties-database.csv", "sha256": "b" * 64},
                }
            )

    def test_explicit_null_interest_rate_is_accepted(self):
        files = LeanMetadataFiles.model_validate(
            {
                "market_hours": {"file_path": "market-hours/market-hours-database.json", "sha256": "a" * 64},
                "symbol_properties": {"file_path": "symbol-properties/symbol-properties-database.csv", "sha256": "b" * 64},
                "interest_rate": None,
            }
        )
        assert files.interest_rate is None

    def test_rejects_a_market_hours_entry_pointing_at_another_kinds_canonical_path(self):
        """Codex P2, PR #1884. A receipt with market_hours.file_path pointing
        at the symbol_properties file's real, on-disk path would pass
        verify_bundle cleanly (the hash legitimately matches that file's
        content) even though LEAN always reads market_hours from its own
        fixed canonical location, never from whatever the receipt names."""
        with pytest.raises(Exception, match="market_hours"):
            LeanMetadataFiles.model_validate(
                {
                    "market_hours": {
                        "file_path": "symbol-properties/symbol-properties-database.csv",
                        "sha256": "a" * 64,
                    },
                    "symbol_properties": {
                        "file_path": "symbol-properties/symbol-properties-database.csv",
                        "sha256": "b" * 64,
                    },
                    "interest_rate": None,
                }
            )

    def test_rejects_an_interest_rate_entry_at_the_wrong_path_when_present(self):
        with pytest.raises(Exception, match="interest_rate"):
            LeanMetadataFiles.model_validate(
                {
                    "market_hours": {"file_path": "market-hours/market-hours-database.json", "sha256": "a" * 64},
                    "symbol_properties": {
                        "file_path": "symbol-properties/symbol-properties-database.csv",
                        "sha256": "b" * 64,
                    },
                    "interest_rate": {"file_path": "alternative/interest-rate/usa/wrong.csv", "sha256": "c" * 64},
                }
            )


class TestMetadataFileEntry:
    @pytest.mark.parametrize("bad_path", ["/etc/passwd", "../../etc/passwd", "a/../../b", "", "."])
    def test_rejects_traversal_and_absolute_paths(self, bad_path):
        with pytest.raises(Exception, match="relative path"):
            MetadataFileEntry(file_path=bad_path, sha256="a" * 64)

    def test_accepts_a_plain_relative_path(self):
        entry = MetadataFileEntry(file_path="market-hours/market-hours-database.json", sha256="a" * 64)
        assert entry.file_path == "market-hours/market-hours-database.json"


class TestReadReceipt:
    def test_returns_none_when_no_receipt_file_exists(self, tmp_path: Path):
        assert read_receipt(tmp_path) is None

    def test_raises_on_malformed_json(self, tmp_path: Path):
        receipt_path(tmp_path).write_text("{not json")
        with pytest.raises(MetadataBundleError, match="malformed"):
            read_receipt(tmp_path)

    def test_raises_on_invalid_utf8_bytes(self, tmp_path: Path):
        """Codex P2, PR #1884. Path.read_text() can raise UnicodeDecodeError
        (a ValueError subclass, not OSError) for a partially-overwritten or
        corrupted receipt file -- that must read as "malformed receipt,
        needs repair" like every other corruption case here, not propagate
        unhandled."""
        receipt_path(tmp_path).write_bytes(b"\xff\xfe\x00\x01not valid utf-8")
        with pytest.raises(MetadataBundleError, match="malformed"):
            read_receipt(tmp_path)

    def test_raises_on_wrong_schema_version(self, tmp_path: Path):
        payload = {
            "schema_version": 2,
            "data_root_id": str(_ROOT_A),
            "price_adjustment_mode": "raw",
            "lean_image_digest": "sha256:abc",
            "files": {
                "market_hours": {"file_path": "market-hours/market-hours-database.json", "sha256": "a" * 64},
                "symbol_properties": {"file_path": "symbol-properties/symbol-properties-database.csv", "sha256": "b" * 64},
                "interest_rate": None,
            },
        }
        receipt_path(tmp_path).write_text(json.dumps(payload))
        with pytest.raises(MetadataBundleError, match="schema_version"):
            read_receipt(tmp_path)

    def test_parses_a_well_formed_receipt(self, tmp_path: Path):
        seed_lean_metadata_receipt(
            tmp_path, data_root_id=_ROOT_A, price_adjustment_mode="raw", lean_image_digest="sha256:abc"
        )
        receipt = read_receipt(tmp_path)
        assert receipt is not None
        assert receipt.schema_version == RECEIPT_SCHEMA_VERSION
        assert receipt.data_root_id == _ROOT_A
        assert receipt.price_adjustment_mode == "raw"
        assert receipt.lean_image_digest == "sha256:abc"
        assert receipt.files.interest_rate is None


class TestVerifyReceiptIdentity:
    def _receipt(self, **overrides) -> LeanMetadataReceipt:
        base = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "data_root_id": _ROOT_A,
            "price_adjustment_mode": "raw",
            "lean_image_digest": "sha256:abc",
            "files": LeanMetadataFiles(
                market_hours=MetadataFileEntry(file_path="market-hours/market-hours-database.json", sha256="a" * 64),
                symbol_properties=MetadataFileEntry(
                    file_path="symbol-properties/symbol-properties-database.csv", sha256="b" * 64
                ),
                interest_rate=None,
            ),
        }
        base.update(overrides)
        return LeanMetadataReceipt(**base)

    def test_accepts_a_matching_receipt(self):
        verify_receipt_identity(
            self._receipt(), expected_root_id=_ROOT_A, expected_mode="raw", expected_digest="sha256:abc"
        )

    def test_rejects_a_receipt_from_another_root(self):
        with pytest.raises(MetadataBundleError, match="data_root_id"):
            verify_receipt_identity(
                self._receipt(), expected_root_id=_ROOT_B, expected_mode="raw", expected_digest="sha256:abc"
            )

    def test_rejects_a_receipt_from_another_mode(self):
        with pytest.raises(MetadataBundleError, match="price_adjustment_mode"):
            verify_receipt_identity(
                self._receipt(),
                expected_root_id=_ROOT_A,
                expected_mode="polygon_split_adjusted",
                expected_digest="sha256:abc",
            )

    def test_rejects_a_receipt_with_a_different_digest(self):
        with pytest.raises(MetadataBundleError, match="lean_image_digest"):
            verify_receipt_identity(
                self._receipt(), expected_root_id=_ROOT_A, expected_mode="raw", expected_digest="sha256:def"
            )


class TestVerifyFilesOnDisk:
    def test_raises_when_a_required_file_is_absent(self, tmp_path: Path):
        seed_lean_metadata_receipt(
            tmp_path, data_root_id=_ROOT_A, price_adjustment_mode="raw", lean_image_digest="sha256:abc"
        )
        receipt = read_receipt(tmp_path)
        (tmp_path / "market-hours" / "market-hours-database.json").unlink()

        with pytest.raises(MetadataBundleError, match="absent"):
            verify_files_on_disk(tmp_path, receipt)

    def test_raises_when_a_file_has_been_tampered_with(self, tmp_path: Path):
        """Acceptance criterion: tampering with a metadata file is detected."""
        seed_lean_metadata_receipt(
            tmp_path, data_root_id=_ROOT_A, price_adjustment_mode="raw", lean_image_digest="sha256:abc"
        )
        receipt = read_receipt(tmp_path)
        (tmp_path / "symbol-properties" / "symbol-properties-database.csv").write_bytes(b"tampered,bytes\n")

        with pytest.raises(MetadataBundleError, match="tampering or corruption"):
            verify_files_on_disk(tmp_path, receipt)

    def test_interest_rate_null_needs_no_file_on_disk(self, tmp_path: Path):
        seed_lean_metadata_receipt(
            tmp_path, data_root_id=_ROOT_A, price_adjustment_mode="raw", lean_image_digest="sha256:abc"
        )
        receipt = read_receipt(tmp_path)
        assert receipt.files.interest_rate is None
        verify_files_on_disk(tmp_path, receipt)  # must not raise

    def test_interest_rate_present_is_also_verified(self, tmp_path: Path):
        seed_lean_metadata_receipt(
            tmp_path,
            data_root_id=_ROOT_A,
            price_adjustment_mode="raw",
            lean_image_digest="sha256:abc",
            include_interest_rate=True,
        )
        receipt = read_receipt(tmp_path)
        assert receipt.files.interest_rate is not None
        (tmp_path / "alternative" / "interest-rate" / "usa" / "interest-rate.csv").write_bytes(b"tampered\n")

        with pytest.raises(MetadataBundleError, match="tampering or corruption"):
            verify_files_on_disk(tmp_path, receipt)


class TestVerifyBundle:
    def test_raises_when_no_receipt_exists(self, tmp_path: Path):
        with pytest.raises(MetadataBundleError, match="no LEAN metadata receipt"):
            verify_bundle(tmp_path, expected_root_id=_ROOT_A, expected_mode="raw", expected_digest="sha256:abc")

    def test_succeeds_for_a_valid_untampered_bundle(self, tmp_path: Path):
        seed_lean_metadata_receipt(
            tmp_path, data_root_id=_ROOT_A, price_adjustment_mode="raw", lean_image_digest="sha256:abc"
        )
        receipt = verify_bundle(tmp_path, expected_root_id=_ROOT_A, expected_mode="raw", expected_digest="sha256:abc")
        assert receipt.lean_image_digest == "sha256:abc"


class TestMetadataDataContractHash:
    def test_varies_with_digest(self):
        a = metadata_data_contract_hash("sha256:aaa", "market-hours-database.json", "raw")
        b = metadata_data_contract_hash("sha256:bbb", "market-hours-database.json", "raw")
        assert a != b

    def test_varies_with_mode(self):
        """The mode is folded into the hash so each mode's physical copy
        gets its own catalog row -- see the function's own docstring."""
        raw = metadata_data_contract_hash("sha256:aaa", "market-hours-database.json", "raw")
        adjusted = metadata_data_contract_hash("sha256:aaa", "market-hours-database.json", "polygon_split_adjusted")
        assert raw != adjusted

    def test_is_deterministic(self):
        a = metadata_data_contract_hash("sha256:aaa", "market-hours-database.json", "raw")
        b = metadata_data_contract_hash("sha256:aaa", "market-hours-database.json", "raw")
        assert a == b


class TestClaimAndCompleteMetadataRowReclaimRace:
    """Ported from the pre-#1879 ``ensure_data`` test of the same shape
    (`_bootstrap_metadata_artifact`'s reclaim dance): the catalog-activation
    race it guards against is a property of the catalog primitives shared
    across every artifact kind, unrelated to how many launcher calls
    preceded it. No Postgres needed: ``catalog_client`` is faked directly so
    the race is deterministic rather than relying on real concurrent
    connections."""

    @pytest.mark.asyncio
    async def test_does_not_misreport_a_lost_reclaim_race_as_exhausted(self, tmp_path: Path, monkeypatch):
        """Regression (review round on #1867, preserved by #1879): a caller
        that loses a reclaim race used to trust `row_state` — a snapshot
        taken *before* the reclaim attempt — instead of re-reading current
        state. Two callers can both see the same settled 'failed' row, both
        attempt `steal_or_retry_minute_bar`, and only one wins; the loser's
        `row_state.status` is still `'failed'` even though the winner just
        flipped the real row to `'fetching'` under a live lease. Trusting
        the stale snapshot reported a terminal, exhausted-retries
        `fetch_timeout` for a row someone else was actively completing —
        this must report the transient `lease_timeout` instead."""
        from app.data_lake.catalog_client import ArtifactClaimState
        from app.data_lake.metadata_bundle import _claim_and_complete_metadata_row

        mh_path = tmp_path / "market-hours" / "market-hours-database.json"
        mh_path.parent.mkdir(parents=True)
        mh_path.write_bytes(b'{"entries": {}}\n')
        entry = MetadataFileEntry(
            file_path="market-hours/market-hours-database.json",
            sha256=hashlib.sha256(mh_path.read_bytes()).hexdigest(),
        )

        stale_snapshot = ArtifactClaimState(id=42, status="failed", attempt_count=1, last_error="boom")
        fresh_after_race = ArtifactClaimState(id=42, status="fetching", attempt_count=2, last_error=None)
        claim_state_calls = {"n": 0}

        async def fake_claim_metadata_artifact(**_kwargs):
            return None  # lost the initial insert — the row already exists

        async def fake_select_complete_metadata_artifact(*_args, **_kwargs):
            return None  # not a cache hit

        async def fake_select_metadata_claim_state(*_args, **_kwargs):
            claim_state_calls["n"] += 1
            # 1st read: the stale snapshot both racing callers observe.
            # 2nd read: this caller re-checking after losing the reclaim
            # below — must see what the winner actually left behind.
            return stale_snapshot if claim_state_calls["n"] == 1 else fresh_after_race

        async def fake_steal_or_retry_minute_bar(**_kwargs):
            return None  # this caller lost the race (issue #1888: bool -> int | None)

        monkeypatch.setattr(catalog_client, "claim_metadata_artifact", fake_claim_metadata_artifact)
        monkeypatch.setattr(catalog_client, "select_complete_metadata_artifact", fake_select_complete_metadata_artifact)
        monkeypatch.setattr(catalog_client, "select_metadata_claim_state", fake_select_metadata_claim_state)
        monkeypatch.setattr(catalog_client, "steal_or_retry_minute_bar", fake_steal_or_retry_minute_bar)

        outcome = await _claim_and_complete_metadata_row(
            spec=_spec(), kind="market_hours", entry=entry, lake_root=tmp_path, root_id=_ROOT_A
        )

        assert outcome.record is None
        assert outcome.is_reused is False
        assert outcome.failure_reason == "lease_timeout", (
            f"lost a reclaim race must read as transient contention, not exhausted retries: got {outcome.failure_reason!r}"
        )
        assert claim_state_calls["n"] == 2, "must re-read claim state after losing the reclaim, not trust the stale snapshot"


class TestBundleLockTimeout:
    """Codex P2, PR #1884. No Postgres needed: lock contention is reproduced
    directly with a real flock held by this test, with the lock's own
    timeout/poll knobs monkeypatched down so the test doesn't actually wait
    out the real 60s budget."""

    @pytest.mark.asyncio
    async def test_bundle_lock_raises_a_distinct_lock_timeout(self, tmp_path: Path, monkeypatch):
        """_bundle_lock itself must raise MetadataBundleLockTimeout, not the
        plain MetadataBundleError -- the two mean different things (lock
        contention vs. an untrustworthy on-disk bundle) and only the former
        may be swallowed into a retryable outcome by the caller."""
        from app.data_lake import metadata_bundle
        from app.utils.advisory_lock import try_advisory_file_lock

        monkeypatch.setattr(metadata_bundle, "_LOCK_TIMEOUT_S", 0.05)
        monkeypatch.setattr(metadata_bundle, "_LOCK_POLL_INTERVAL_S", 0.01)
        target = receipt_path(tmp_path)

        with try_advisory_file_lock(target) as acquired:
            assert acquired is True
            with pytest.raises(metadata_bundle.MetadataBundleLockTimeout):
                async with metadata_bundle._bundle_lock(tmp_path):
                    pytest.fail("must not acquire the lock while it is already held")

    @pytest.mark.asyncio
    async def test_ensure_lean_metadata_bundle_reports_lease_timeout_on_lock_contention(
        self, tmp_path: Path, monkeypatch
    ):
        """A lock-timeout reaching ensure_lean_metadata_bundle must come back
        as the same structured, retryable MetadataBundleOutcome the module
        already uses for other "still contended" conditions -- never an
        unhandled exception. Never reaches Postgres or the launcher: the
        lock times out before the guarded body runs."""
        from app.data_lake import metadata_bundle
        from app.utils.advisory_lock import try_advisory_file_lock

        monkeypatch.setattr(metadata_bundle, "_LOCK_TIMEOUT_S", 0.05)
        monkeypatch.setattr(metadata_bundle, "_LOCK_POLL_INTERVAL_S", 0.01)
        lake_root = tmp_path / "lake"
        lake_root.mkdir()
        staging_root = tmp_path / "staging"
        staging_root.mkdir()

        with try_advisory_file_lock(receipt_path(lake_root)) as acquired:
            assert acquired is True
            outcome = await ensure_lean_metadata_bundle(_spec(), lake_root, staging_root)

        assert outcome.market_hours == MetadataBootstrap(None, False, "lease_timeout")
        assert outcome.symbol_properties == MetadataBootstrap(None, False, "lease_timeout")
        assert outcome.interest_rate == MetadataBootstrap(None, False, "lease_timeout")


# ---------------------------------------------------------------------------
# ensure_lean_metadata_bundle end-to-end -- Postgres + mocked launcher
# ---------------------------------------------------------------------------


def _postgres_url() -> str:
    url = settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured")
    return url


@pytest.fixture
async def clean_artifacts():
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute('TRUNCATE TABLE "DataLakeArtifacts" RESTART IDENTITY CASCADE')
    finally:
        await conn.close()
    yield
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute('TRUNCATE TABLE "DataLakeArtifacts" RESTART IDENTITY CASCADE')
    finally:
        await conn.close()


@pytest.fixture
async def pool():
    await catalog_client.close_pool()
    await catalog_client.init_pool()
    yield
    await catalog_client.close_pool()


@pytest.fixture
def tmp_lake(tmp_path: Path, monkeypatch):
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
    return artifacts_root


_MARKET_HOURS_JSON = json.dumps({"entries": {}}).encode("utf-8")
_SYMBOL_PROPERTIES_CSV = b"SPY,equity,usd,1,0\n"


_INTEREST_RATE_CSV = b"date,rate\n"


def _stage_workspace_files(artifacts_root: Path, run_id: str, *, digest: str, include_interest_rate: bool = False) -> None:
    """Content is keyed by ``digest`` so a re-extraction under a new digest
    is distinguishable on disk from the previous one."""
    data_dir = artifacts_root / run_id / "workspace" / "data"
    (data_dir / "market-hours").mkdir(parents=True, exist_ok=True)
    (data_dir / "symbol-properties").mkdir(parents=True, exist_ok=True)
    (data_dir / "market-hours" / "market-hours-database.json").write_bytes(_MARKET_HOURS_JSON + digest.encode())
    (data_dir / "symbol-properties" / "symbol-properties-database.csv").write_bytes(_SYMBOL_PROPERTIES_CSV + digest.encode())
    if include_interest_rate:
        ir_dir = data_dir / "alternative" / "interest-rate" / "usa"
        ir_dir.mkdir(parents=True, exist_ok=True)
        (ir_dir / "interest-rate.csv").write_bytes(_INTEREST_RATE_CSV + digest.encode())


def _launcher_side_effect(artifacts_root: Path, *, interest_rate_digests: frozenset[str] = frozenset()):
    """``interest_rate_digests`` opts specific digests into carrying
    interest-rate bytes -- mirroring how a real LEAN image variant either
    does or doesn't bundle the ``alternative/interest-rate`` subtree, so one
    mocked launcher route can serve two calls for two digests differently."""

    def _mock(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        digest = body["image_digest"]
        _stage_workspace_files(
            artifacts_root, body["run_id"], digest=digest, include_interest_rate=digest in interest_rate_digests
        )
        return httpx.Response(
            200,
            json={
                "market_hours_db_path": "/launcher-side/market-hours-database.json",
                "symbol_properties_db_path": "/launcher-side/symbol-properties-database.csv",
            },
        )

    return _mock


def _spec(*, lean_image_digest: str = "sha256:test", request_id: UUID | None = None) -> DataRunSpec:
    from datetime import date

    from app.data_lake.types import trading_date_to_calendar_anchor_ms

    return DataRunSpec(
        request_id=request_id or uuid4(),
        run_type="python_lab",
        symbols=["SPY"],
        start_trading_date_ms=trading_date_to_calendar_anchor_ms(date(2024, 5, 20)),
        end_trading_date_ms=trading_date_to_calendar_anchor_ms(date(2024, 5, 24)),
        lean_image_digest=lean_image_digest,
    )


@respx.mock
@pytest.mark.asyncio
async def test_one_launcher_call_serves_the_whole_bundle(clean_artifacts, pool, tmp_lake):
    """Avoid re-invoking LEAN extraction per individual file: one
    ensure_lean_metadata_bundle call makes exactly one launcher call, not
    one per file (mh, sp, ir)."""
    from app.data_lake.path_policy import resolve_lake_root, resolve_staging_root

    launcher_route = respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake)
    )
    lake_root = resolve_lake_root("raw")
    staging_root = resolve_staging_root("raw")
    lake_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)

    outcome = await ensure_lean_metadata_bundle(_spec(), lake_root, staging_root)

    assert launcher_route.call_count == 1
    assert outcome.market_hours.record is not None
    assert outcome.symbol_properties.record is not None
    assert outcome.interest_rate.failure_reason == "provider_no_data"
    assert receipt_path(lake_root).is_file()


@respx.mock
@pytest.mark.asyncio
async def test_second_call_with_the_same_digest_is_a_pure_cache_hit(clean_artifacts, pool, tmp_lake):
    from app.data_lake.path_policy import resolve_lake_root, resolve_staging_root

    launcher_route = respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake)
    )
    lake_root = resolve_lake_root("raw")
    staging_root = resolve_staging_root("raw")
    lake_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)

    first = await ensure_lean_metadata_bundle(_spec(), lake_root, staging_root)
    second = await ensure_lean_metadata_bundle(_spec(), lake_root, staging_root)

    assert launcher_route.call_count == 1, "the second call must not re-extract"
    assert second.market_hours.is_reused is True
    assert second.market_hours.record.id == first.market_hours.record.id


@respx.mock
@pytest.mark.asyncio
async def test_changing_the_digest_triggers_re_extraction_and_stales_the_old_row(clean_artifacts, pool, tmp_lake):
    """Acceptance criterion: changing lean_image_digest triggers
    re-extraction even when an older catalog row is complete, and the old
    row is marked stale rather than left claiming the (now-overwritten)
    physical path."""
    from app.data_lake.path_policy import resolve_lake_root, resolve_staging_root

    launcher_route = respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake)
    )
    lake_root = resolve_lake_root("raw")
    staging_root = resolve_staging_root("raw")
    lake_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)

    first = await ensure_lean_metadata_bundle(_spec(lean_image_digest="sha256:aaa"), lake_root, staging_root)
    second = await ensure_lean_metadata_bundle(_spec(lean_image_digest="sha256:bbb"), lake_root, staging_root)

    assert launcher_route.call_count == 2
    assert second.market_hours.record.id != first.market_hours.record.id
    receipt = read_receipt(lake_root)
    assert receipt.lean_image_digest == "sha256:bbb"

    async with catalog_client.connection() as conn:
        row = await conn.fetchrow(
            'SELECT "Status" FROM "DataLakeArtifacts" WHERE "Id" = $1', first.market_hours.record.id
        )
    assert row["Status"] == "stale"


@respx.mock
@pytest.mark.asyncio
async def test_a_receipt_from_another_root_is_rejected_and_repaired(clean_artifacts, pool, tmp_lake):
    """Acceptance criterion: a receipt from another root is rejected. Seeded
    directly on disk (no launcher call), standing in for a lake root that
    was remounted at the wrong physical volume."""
    from app.data_lake.path_policy import resolve_lake_root, resolve_staging_root

    launcher_route = respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake)
    )
    lake_root = resolve_lake_root("raw")
    staging_root = resolve_staging_root("raw")
    lake_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)
    seed_lean_metadata_receipt(
        lake_root, data_root_id=_ROOT_B, price_adjustment_mode="raw", lean_image_digest="sha256:test"
    )

    outcome = await ensure_lean_metadata_bundle(_spec(lean_image_digest="sha256:test"), lake_root, staging_root)

    assert launcher_route.call_count == 1, "a foreign-root receipt must not be trusted; must repair by re-extracting"
    receipt = read_receipt(lake_root)
    assert receipt.data_root_id != _ROOT_B
    assert outcome.market_hours.record is not None


@respx.mock
@pytest.mark.asyncio
async def test_tampered_file_is_not_reused_and_is_repaired(clean_artifacts, pool, tmp_lake):
    """Acceptance criterion: tampering with a metadata file is detected
    before launch. Here at the writer's own reuse check: a tampered file
    forces repair rather than a silent, corrupted cache hit."""
    from app.data_lake.path_policy import resolve_lake_root, resolve_staging_root

    launcher_route = respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake)
    )
    lake_root = resolve_lake_root("raw")
    staging_root = resolve_staging_root("raw")
    lake_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)

    await ensure_lean_metadata_bundle(_spec(), lake_root, staging_root)
    (lake_root / "symbol-properties" / "symbol-properties-database.csv").write_bytes(b"tampered\n")

    outcome = await ensure_lean_metadata_bundle(_spec(), lake_root, staging_root)

    assert launcher_route.call_count == 2, "tampering must force a fresh extraction, not a silent reuse"
    receipt = read_receipt(lake_root)
    actual = hashlib.sha256((lake_root / "symbol-properties" / "symbol-properties-database.csv").read_bytes()).hexdigest()
    assert receipt.files.symbol_properties.sha256 == actual
    assert outcome.symbol_properties.record is not None


@respx.mock
@pytest.mark.asyncio
async def test_a_crash_before_receipt_publication_cannot_produce_a_false_cache_hit(clean_artifacts, pool, tmp_lake):
    """Acceptance criterion. A completed catalog row plus files on disk but
    NO receipt (simulating a crash between file publication and the
    receipt write, or -- as reproduced here -- the receipt vanishing after
    a successful run) must never be treated as a valid cache hit."""
    from app.data_lake.path_policy import resolve_lake_root, resolve_staging_root

    launcher_route = respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake)
    )
    lake_root = resolve_lake_root("raw")
    staging_root = resolve_staging_root("raw")
    lake_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)

    first = await ensure_lean_metadata_bundle(_spec(), lake_root, staging_root)
    assert first.market_hours.record is not None
    receipt_path(lake_root).unlink()  # the missing commit marker a pre-receipt crash would leave

    second = await ensure_lean_metadata_bundle(_spec(), lake_root, staging_root)

    assert launcher_route.call_count == 2, "a missing receipt must never read as a cache hit, complete catalog row or not"
    assert receipt_path(lake_root).is_file(), "the call must repair by republishing the receipt"
    assert second.market_hours.record is not None


@respx.mock
@pytest.mark.asyncio
async def test_concurrent_different_digest_requests_do_not_interleave_files_and_receipt(clean_artifacts, pool, tmp_lake):
    """Acceptance criterion: concurrent requests for different digests
    cannot interleave files and receipts. After both complete, the files on
    disk must fully match whichever digest ended up in the receipt -- never
    a mix of one digest's receipt with another digest's file bytes."""
    from app.data_lake.path_policy import resolve_lake_root, resolve_staging_root

    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake)
    )
    lake_root = resolve_lake_root("raw")
    staging_root = resolve_staging_root("raw")
    lake_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)

    await asyncio.gather(
        ensure_lean_metadata_bundle(_spec(lean_image_digest="sha256:aaa"), lake_root, staging_root),
        ensure_lean_metadata_bundle(_spec(lean_image_digest="sha256:bbb"), lake_root, staging_root),
    )

    receipt = read_receipt(lake_root)
    assert receipt.lean_image_digest in {"sha256:aaa", "sha256:bbb"}
    verify_bundle(  # must not raise: whichever digest won, its files are fully self-consistent
        lake_root, expected_root_id=receipt.data_root_id, expected_mode="raw", expected_digest=receipt.lean_image_digest
    )


@respx.mock
@pytest.mark.asyncio
async def test_transitioning_to_no_interest_rate_data_removes_the_stale_file_and_its_row(clean_artifacts, pool, tmp_lake):
    """Codex P1, PR #1884. Digest A publishes interest-rate data; digest B
    (the next extraction at this lake root) has none. The module's own
    docstring says LEAN discovers the interest-rate file by opening the
    fixed canonical path directly on the mounted filesystem -- it does not
    consult the receipt to decide whether to look. A file left over from A
    would therefore be silently read by LEAN even though B's receipt
    correctly records interest_rate: null. Both the stale file and A's
    now-orphaned interest-rate catalog row must be cleaned up."""
    from app.data_lake.path_policy import resolve_lake_root, resolve_staging_root

    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake, interest_rate_digests=frozenset({"sha256:aaa"}))
    )
    lake_root = resolve_lake_root("raw")
    staging_root = resolve_staging_root("raw")
    lake_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)
    ir_path = lake_root / "alternative" / "interest-rate" / "usa" / "interest-rate.csv"

    first = await ensure_lean_metadata_bundle(_spec(lean_image_digest="sha256:aaa"), lake_root, staging_root)
    assert first.interest_rate.record is not None
    assert ir_path.is_file()
    first_ir_id = first.interest_rate.record.id

    second = await ensure_lean_metadata_bundle(_spec(lean_image_digest="sha256:bbb"), lake_root, staging_root)

    assert second.interest_rate.failure_reason == "provider_no_data"
    assert not ir_path.exists(), "the interest-rate file left over from digest A must be deleted"
    receipt = read_receipt(lake_root)
    assert receipt.files.interest_rate is None

    async with catalog_client.connection() as conn:
        row = await conn.fetchrow('SELECT "Status" FROM "DataLakeArtifacts" WHERE "Id" = $1', first_ir_id)
    assert row["Status"] != "complete", "digest A's interest-rate row must no longer read as complete"


@respx.mock
@pytest.mark.asyncio
async def test_rolling_back_to_a_staled_digest_reactivates_its_row_instead_of_lease_timeout(
    clean_artifacts, pool, tmp_lake
):
    """Codex P1, PR #1884 -- the most important regression here. Once digest
    B's bundle completes, mark_metadata_artifacts_stale_for_path marks
    digest A's now-superseded row 'stale' (metadata FilePath is canonical
    and mode-relative, not digest-specific, so both digests' rows claim the
    same physical path). An operator rollback to digest A re-extracts (the
    receipt now names B, so verify_bundle no longer trusts it) and
    re-publishes A's bytes to disk -- but before this fix,
    claim_metadata_artifact's ON CONFLICT hits A's existing (now 'stale')
    row and returns None; select_complete_metadata_artifact finds nothing
    (status is 'stale', not 'complete'); and steal_or_retry_minute_bar's
    WHERE clause matched neither 'fetching' nor 'failed', so the row could
    never be reclaimed -- ensure_lean_metadata_bundle reported
    lease_timeout permanently, on every subsequent retry, even though this
    exact call had already re-verified digest A's bytes on disk moments
    earlier."""
    from app.data_lake.path_policy import resolve_lake_root, resolve_staging_root

    launcher_route = respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_launcher_side_effect(tmp_lake)
    )
    lake_root = resolve_lake_root("raw")
    staging_root = resolve_staging_root("raw")
    lake_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)

    first = await ensure_lean_metadata_bundle(_spec(lean_image_digest="sha256:aaa"), lake_root, staging_root)
    await ensure_lean_metadata_bundle(_spec(lean_image_digest="sha256:bbb"), lake_root, staging_root)

    async with catalog_client.connection() as conn:
        precondition = await conn.fetchrow(
            'SELECT "Status" FROM "DataLakeArtifacts" WHERE "Id" = $1', first.market_hours.record.id
        )
    assert precondition["Status"] == "stale", "sanity check: reproduces the precondition this fix reactivates from"

    rollback = await ensure_lean_metadata_bundle(_spec(lean_image_digest="sha256:aaa"), lake_root, staging_root)

    assert launcher_route.call_count == 3, "rollback must re-extract -- the on-disk receipt now names digest B"
    assert rollback.market_hours.failure_reason is None, (
        f"a staled row for the exact digest whose bytes were just re-verified on disk must reactivate, "
        f"not report lease_timeout forever: got {rollback.market_hours.failure_reason!r}"
    )
    assert rollback.market_hours.record is not None
    assert rollback.market_hours.record.id == first.market_hours.record.id, "reactivates the SAME row, not a new one"

    async with catalog_client.connection() as conn:
        row = await conn.fetchrow('SELECT "Status" FROM "DataLakeArtifacts" WHERE "Id" = $1', first.market_hours.record.id)
    assert row["Status"] == "complete"


# ---------------------------------------------------------------------------
# #1889: launcher-unreachable is transient, distinct from a genuine
# extraction failure, surfaces through the existing typed diagnostic, and
# stays retryable indefinitely rather than latching after a single attempt.
# ---------------------------------------------------------------------------


def _unreachable_launcher_side_effect(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("Connection refused")


@respx.mock
@pytest.mark.asyncio
async def test_launcher_unreachable_is_recorded_as_transient_and_names_the_launcher(clean_artifacts, pool, tmp_lake):
    """Acceptance criteria (a) + (c): a launcher-unreachable extraction
    failure must leave a retryable, auditable catalog row (not a silent
    no-op and not a terminal reason), and the surfaced detail must name the
    launcher explicitly rather than reading as a generic provider error."""
    from app.data_lake.path_policy import resolve_lake_root, resolve_staging_root

    launcher_route = respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_unreachable_launcher_side_effect
    )
    lake_root = resolve_lake_root("raw")
    staging_root = resolve_staging_root("raw")
    lake_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)

    outcome = await ensure_lean_metadata_bundle(_spec(), lake_root, staging_root)

    assert launcher_route.call_count == 1
    for bootstrap in (outcome.market_hours, outcome.symbol_properties, outcome.interest_rate):
        assert bootstrap.record is None
        assert bootstrap.failure_reason == "launcher_unreachable", (
            f"launcher-unreachable must be its own transient reason, distinct from io_error: {bootstrap.failure_reason!r}"
        )
        assert bootstrap.detail is not None and "launcher" in bootstrap.detail.lower(), (
            f"the surfaced detail must name the launcher: {bootstrap.detail!r}"
        )
        assert "unreachable" in bootstrap.detail.lower()

    # The failure must be a real, auditable catalog row -- not silently
    # dropped -- with AttemptCount and LastError recorded like every other
    # artifact kind's failure (catalog_client.fail_artifact's own contract).
    async with catalog_client.connection() as conn:
        rows = await conn.fetch(
            """SELECT "Status", "AttemptCount", "LastError" FROM "DataLakeArtifacts"
               WHERE "ArtifactKind" = 'metadata'"""
        )
    assert len(rows) == 3, f"expected all three metadata kinds claimed as failed rows, got {len(rows)}"
    for row in rows:
        assert row["Status"] == "failed", "left retryable (Status='failed'), not silently absent from the catalog"
        assert row["AttemptCount"] == 1
        assert row["LastError"] == "launcher_unreachable"


@respx.mock
@pytest.mark.asyncio
async def test_launcher_unreachable_failure_is_retried_once_the_launcher_recovers(clean_artifacts, pool, tmp_lake):
    """Acceptance criterion (b): a subsequent materialization must actually
    re-attempt -- and this time complete -- a metadata artifact left
    'failed' by an unreachable launcher, rather than skipping it forever
    because it's already 'failed'."""
    from app.data_lake.path_policy import resolve_lake_root, resolve_staging_root

    stage = _launcher_side_effect(tmp_lake)
    calls = {"n": 0}

    def _fails_once_then_recovers(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("Connection refused")
        return stage(request)

    launcher_route = respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_fails_once_then_recovers
    )
    lake_root = resolve_lake_root("raw")
    staging_root = resolve_staging_root("raw")
    lake_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)

    first = await ensure_lean_metadata_bundle(_spec(), lake_root, staging_root)
    assert first.market_hours.failure_reason == "launcher_unreachable"

    second = await ensure_lean_metadata_bundle(_spec(), lake_root, staging_root)

    assert launcher_route.call_count == 2, "the recovered launcher must actually be retried, not skipped"
    assert second.market_hours.record is not None
    assert second.market_hours.failure_reason is None
    assert second.symbol_properties.record is not None

    async with catalog_client.connection() as conn:
        row = await conn.fetchrow(
            'SELECT "Status", "AttemptCount" FROM "DataLakeArtifacts" WHERE "Id" = $1', second.market_hours.record.id
        )
    assert row["Status"] == "complete"
    assert row["AttemptCount"] == 2, "the reclaim on the successful retry must bump AttemptCount, not start a new row"


@respx.mock
@pytest.mark.asyncio
async def test_a_failed_repair_does_not_report_unverifiable_metadata_as_reused(clean_artifacts, pool, tmp_lake):
    """A complete catalog row is not, by itself, evidence the bytes exist.

    Sequence: a good bundle is published and catalogued; a file is then
    tampered with, so ``verify_bundle`` rejects the bundle; the repair
    extraction is attempted and fails because the launcher is unreachable.
    The three catalog rows are still 'complete' from the first run, and the
    failure path used to adopt them and report the metadata as successfully
    reused -- so ``ensure_data`` returned success for metadata whose bytes
    had just been proven unusable, and the run proceeded toward a LEAN mount
    that could not verify.
    """
    from app.data_lake.path_policy import resolve_lake_root, resolve_staging_root

    stage = _launcher_side_effect(tmp_lake)
    calls = {"n": 0}

    def _succeeds_once_then_unreachable(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return stage(request)
        raise httpx.ConnectError("Connection refused")

    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_succeeds_once_then_unreachable
    )
    lake_root = resolve_lake_root("raw")
    staging_root = resolve_staging_root("raw")
    lake_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)

    first = await ensure_lean_metadata_bundle(_spec(), lake_root, staging_root)
    assert first.market_hours.record is not None, "the first run must publish and catalogue a good bundle"
    assert first.symbol_properties.record is not None

    # The bytes the catalog rows describe are now unusable.
    (lake_root / "symbol-properties" / "symbol-properties-database.csv").write_bytes(b"tampered\n")

    second = await ensure_lean_metadata_bundle(_spec(), lake_root, staging_root)

    for kind, bootstrap in (
        ("market_hours", second.market_hours),
        ("symbol_properties", second.symbol_properties),
        ("interest_rate", second.interest_rate),
    ):
        assert bootstrap.record is None, (
            f"{kind}: metadata was reported as available on the strength of a stale catalog row, "
            f"but the bundle it describes failed verification and the repair extraction failed"
        )
        assert bootstrap.failure_reason == "launcher_unreachable", (
            f"{kind}: the extraction failure must be surfaced, not replaced by a reuse"
        )


@respx.mock
@pytest.mark.asyncio
async def test_a_failed_repair_still_reuses_a_bundle_that_verifies(clean_artifacts, pool, tmp_lake):
    """The other half: refusing to adopt a stale row must not turn every
    launcher outage into a failure. When the bundle on disk verifies, the
    completed rows are genuinely usable and are still reused -- an outage
    with nothing wrong on disk is a cache hit, not an error."""
    from app.data_lake.path_policy import resolve_lake_root, resolve_staging_root

    stage = _launcher_side_effect(tmp_lake)
    calls = {"n": 0}

    def _succeeds_once_then_unreachable(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return stage(request)
        raise httpx.ConnectError("Connection refused")

    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_succeeds_once_then_unreachable
    )
    lake_root = resolve_lake_root("raw")
    staging_root = resolve_staging_root("raw")
    lake_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)

    first = await ensure_lean_metadata_bundle(_spec(), lake_root, staging_root)
    assert first.market_hours.record is not None

    second = await ensure_lean_metadata_bundle(_spec(), lake_root, staging_root)

    assert second.market_hours.record is not None, "an intact, verifying bundle must still be a cache hit"
    assert second.market_hours.failure_reason is None
    assert calls["n"] == 1, "a verifying bundle must not have re-called the launcher at all"


@respx.mock
@pytest.mark.asyncio
async def test_launcher_unreachable_never_exhausts_the_retry_ceiling(clean_artifacts, pool, tmp_lake):
    """#1889: unlike a genuine extraction failure, launcher-unreachable must
    stay retryable no matter how many consecutive attempts fail -- an
    operator who restarts the launcher after AttemptCount has passed
    _MAX_CLAIM_RETRIES must still see the artifact retried, not a permanent
    'fetch_timeout' latch (the exact latch this issue exists to remove)."""
    from app.data_lake import metadata_bundle
    from app.data_lake.path_policy import resolve_lake_root, resolve_staging_root

    stage = _launcher_side_effect(tmp_lake)
    calls = {"n": 0}
    failures_before_recovery = metadata_bundle._MAX_CLAIM_RETRIES + 2  # past the normal ceiling

    def _fails_repeatedly_then_recovers(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= failures_before_recovery:
            raise httpx.ConnectError("Connection refused")
        return stage(request)

    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        side_effect=_fails_repeatedly_then_recovers
    )
    lake_root = resolve_lake_root("raw")
    staging_root = resolve_staging_root("raw")
    lake_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)

    for i in range(failures_before_recovery):
        outcome = await ensure_lean_metadata_bundle(_spec(), lake_root, staging_root)
        assert outcome.market_hours.failure_reason == "launcher_unreachable", (
            f"call {i + 1}: must stay classified as transient even past the normal retry ceiling, "
            f"got {outcome.market_hours.failure_reason!r}"
        )

    recovered = await ensure_lean_metadata_bundle(_spec(), lake_root, staging_root)
    assert recovered.market_hours.record is not None, "must still retry and succeed once the launcher recovers"
    assert recovered.market_hours.failure_reason is None


@respx.mock
@pytest.mark.asyncio
async def test_non_launcher_extraction_failure_still_respects_the_retry_ceiling(clean_artifacts, pool, tmp_lake):
    """Regression guard: only launcher_unreachable is exempt from the
    normal retry ceiling. A genuinely, repeatedly failing extraction (a
    malformed launcher response) must still exhaust after
    _MAX_CLAIM_RETRIES attempts and report the existing 'fetch_timeout'
    terminal reason, exactly as every other artifact kind already does --
    the fix must not make every failure infinitely retryable."""
    from app.data_lake import metadata_bundle
    from app.data_lake.path_policy import resolve_lake_root, resolve_staging_root

    respx.post(re.compile(r"http://launcher-mock:8090/extract-metadata")).mock(
        return_value=httpx.Response(500, json={"detail": "launcher internal error"})
    )
    lake_root = resolve_lake_root("raw")
    staging_root = resolve_staging_root("raw")
    lake_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)

    reasons = []
    for _ in range(metadata_bundle._MAX_CLAIM_RETRIES + 1):
        outcome = await ensure_lean_metadata_bundle(_spec(), lake_root, staging_root)
        reasons.append(outcome.market_hours.failure_reason)

    assert reasons[0] == "io_error"
    assert reasons[-1] == "fetch_timeout", (
        f"a genuinely repeated extraction failure must eventually exhaust its retry budget, got {reasons}"
    )
