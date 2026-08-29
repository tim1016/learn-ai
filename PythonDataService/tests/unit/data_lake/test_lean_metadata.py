"""Unit tests for app.data_lake.lean_metadata.

The module POSTs {run_id, image_digest} to the LEAN-sidecar launcher's
/extract-metadata endpoint, then reads the two database files the launcher
wrote back off this process's own view of the shared artifacts bind mount
(the launcher's JSON response carries paths only meaningful on the launcher
host — see app.lean_sidecar.launcher.models.ExtractMetadataResponse and
app.lean_sidecar.staging._stage_lean_metadata_via_launcher, the original
caller this module mirrors). Tests mock the HTTP call and pre-place the
files a real launcher would have written, under a tmp_path artifacts root.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.5
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
import respx

from app.data_lake import lean_metadata
from app.data_lake.lean_metadata import (
    LeanMetadataExtractionError,
    extract_lean_metadata,
)
from app.lean_sidecar.launcher_client import EXTRACT_METADATA_HTTP_TIMEOUT_S

RUN_ID = "metadata-11111111-1111-1111-1111-111111111111"


def _stage_workspace_files(artifacts_root: Path, run_id: str, mh_bytes: bytes, sp_bytes: bytes) -> None:
    """Pre-place the two files a real launcher run would have written.

    Layout must match app.lean_sidecar.workspace.Workspace.data_dir and
    staging.list_metadata_databases exactly: <root>/<run_id>/workspace/data/...
    """
    data_dir = artifacts_root / run_id / "workspace" / "data"
    (data_dir / "market-hours").mkdir(parents=True, exist_ok=True)
    (data_dir / "symbol-properties").mkdir(parents=True, exist_ok=True)
    (data_dir / "market-hours" / "market-hours-database.json").write_bytes(mh_bytes)
    (data_dir / "symbol-properties" / "symbol-properties-database.csv").write_bytes(sp_bytes)


@pytest.mark.asyncio
@respx.mock
async def test_extract_lean_metadata_extracts_market_hours_and_symbol_properties(tmp_path):
    mh_bytes = b'{"exchange": "NYSE", "rule": "..."}'
    sp_bytes = b"SPY,equity,usd,1,0\n"
    _stage_workspace_files(tmp_path, RUN_ID, mh_bytes, sp_bytes)
    respx.post(re.compile(r"http://[^/]+/extract-metadata")).mock(
        return_value=httpx.Response(
            200,
            json={
                "market_hours_db_path": "/irrelevant/launcher-side/path.json",
                "symbol_properties_db_path": "/irrelevant/launcher-side/path.csv",
            },
        )
    )
    market_hours, symbol_properties = await extract_lean_metadata(
        image_digest="sha256:97884667...",
        launcher_url="http://launcher:8090",
        launcher_token="t",
        run_id=RUN_ID,
        artifacts_root=tmp_path,
    )
    assert market_hours == mh_bytes
    assert symbol_properties == sp_bytes


@pytest.mark.asyncio
@respx.mock
async def test_extract_lean_metadata_sends_run_id_the_launcher_requires(tmp_path):
    """Regression: the launcher's ExtractMetadataRequest requires run_id to
    resolve a workspace path (app.lean_sidecar.launcher.models). A prior
    version of this call sent only image_digest, which the launcher rejects
    with a 422 on every real attempt."""
    _stage_workspace_files(tmp_path, RUN_ID, b"x", b"y")
    route = respx.post(re.compile(r"http://[^/]+/extract-metadata")).mock(
        return_value=httpx.Response(
            200,
            json={
                "market_hours_db_path": "/irrelevant.json",
                "symbol_properties_db_path": "/irrelevant.csv",
            },
        )
    )
    await extract_lean_metadata(
        image_digest="sha256:97884667...",
        launcher_url="http://launcher:8090",
        launcher_token="t",
        run_id=RUN_ID,
        artifacts_root=tmp_path,
    )
    import json

    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body["run_id"] == RUN_ID


@pytest.mark.asyncio
@respx.mock
async def test_extract_lean_metadata_raises_on_launcher_500(tmp_path):
    respx.post(re.compile(r"http://[^/]+/extract-metadata")).mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(LeanMetadataExtractionError):
        await extract_lean_metadata(
            image_digest="sha256:97884667...",
            launcher_url="http://launcher:8090",
            launcher_token="t",
            run_id=RUN_ID,
            artifacts_root=tmp_path,
        )


@pytest.mark.asyncio
@respx.mock
async def test_extract_lean_metadata_raises_on_missing_workspace_files(tmp_path):
    """Regression: a prior version of this call parsed base64-encoded bytes
    out of the launcher's JSON response — a transport the launcher has never
    actually implemented (ExtractMetadataResponse carries paths, not bytes).
    A 200 with no files on this side of the shared mount must be a clear
    extraction error, not a KeyError leaking out of response parsing."""
    respx.post(re.compile(r"http://[^/]+/extract-metadata")).mock(
        return_value=httpx.Response(
            200,
            json={
                "market_hours_db_path": "/irrelevant.json",
                "symbol_properties_db_path": "/irrelevant.csv",
            },
        )
    )
    with pytest.raises(LeanMetadataExtractionError):
        await extract_lean_metadata(
            image_digest="sha256:97884667...",
            launcher_url="http://launcher:8090",
            launcher_token="t",
            run_id=RUN_ID,
            artifacts_root=tmp_path,  # no files staged
        )


@pytest.mark.asyncio
@respx.mock
async def test_extract_lean_metadata_raises_on_unreadable_workspace_file(tmp_path):
    """Regression: `read_bytes()` on a workspace file raises plain `OSError`
    — a vanished file, an unreadable mount, a permissions mismatch — not
    `LeanMetadataExtractionError`. Left untranslated, that `OSError` skips
    `_bootstrap_metadata_artifact`'s `except LeanMetadataExtractionError`
    handling entirely and aborts the whole `ensure_data` request instead of
    cleanly failing this one claimed artifact with `io_error`.

    A directory where a file is expected reproduces `OSError`
    (`IsADirectoryError`) deterministically, without depending on filesystem
    permission semantics that vary by platform or by which user runs the
    suite.
    """
    data_dir = tmp_path / RUN_ID / "workspace" / "data"
    (data_dir / "market-hours" / "market-hours-database.json").mkdir(parents=True)
    (data_dir / "symbol-properties").mkdir(parents=True)
    (data_dir / "symbol-properties" / "symbol-properties-database.csv").write_bytes(b"SPY,equity,usd,1,0\n")
    respx.post(re.compile(r"http://[^/]+/extract-metadata")).mock(
        return_value=httpx.Response(
            200,
            json={
                "market_hours_db_path": "/irrelevant.json",
                "symbol_properties_db_path": "/irrelevant.csv",
            },
        )
    )
    with pytest.raises(LeanMetadataExtractionError):
        await extract_lean_metadata(
            image_digest="sha256:97884667...",
            launcher_url="http://launcher:8090",
            launcher_token="t",
            run_id=RUN_ID,
            artifacts_root=tmp_path,
        )


def test_extract_lean_metadata_default_timeout_matches_the_launcher_extraction_budget():
    """Regression: this caller's default HTTP timeout was hardcoded to 60s —
    harmless while a missing `run_id` made the launcher reject every call
    fast, but once `run_id` (see
    `test_extract_lean_metadata_sends_run_id_the_launcher_requires` above)
    makes the launcher actually execute, a legitimate cold
    `podman create + cp x3 + rm` sequence can take up to ~300s
    (`app.lean_sidecar.launcher_client.post_extract_metadata_sync`, the
    canonical caller of this identical endpoint, budgets 360s for exactly
    that). An unmatched, shorter timeout here would spuriously fail a
    legitimate slow extraction instead of waiting it out."""
    assert lean_metadata._DEFAULT_TIMEOUT_S == EXTRACT_METADATA_HTTP_TIMEOUT_S
