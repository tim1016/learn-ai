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

from app.data_lake.lean_metadata import (
    LeanMetadataExtractionError,
    extract_lean_metadata,
)

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
async def test_extracts_market_hours_and_symbol_properties(tmp_path):
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
async def test_sends_run_id_the_launcher_requires(tmp_path):
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
async def test_launcher_500_raises_extraction_error(tmp_path):
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
async def test_missing_workspace_files_raises(tmp_path):
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
