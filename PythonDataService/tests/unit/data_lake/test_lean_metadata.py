"""Unit tests for app.data_lake.lean_metadata.

The module delegates to the LEAN-sidecar launcher's POST /extract-metadata
endpoint (the launcher owns podman access; the data-plane container does
not have podman on PATH). For unit tests we mock the httpx call and assert
the returned bytes are surfaced unchanged.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.5
"""

from __future__ import annotations

import base64
import re

import httpx
import pytest
import respx

from app.data_lake.lean_metadata import (
    LeanMetadataExtractionError,
    extract_lean_metadata,
)


@pytest.mark.asyncio
@respx.mock
async def test_extracts_market_hours_and_symbol_properties():
    mh_bytes = b'{"exchange": "NYSE", "rule": "..."}'
    sp_bytes = b"SPY,equity,usd,1,0\n"
    respx.post(re.compile(r"http://[^/]+/extract-metadata")).mock(
        return_value=httpx.Response(
            200,
            json={
                "market_hours_database_b64": base64.b64encode(mh_bytes).decode("ascii"),
                "symbol_properties_database_b64": base64.b64encode(sp_bytes).decode("ascii"),
                "image_digest_used": "sha256:97884667...",
            },
        )
    )
    market_hours, symbol_properties = await extract_lean_metadata(
        image_digest="sha256:97884667...",
        launcher_url="http://launcher:8090",
        launcher_token="t",
    )
    assert market_hours == mh_bytes
    assert symbol_properties == sp_bytes


@pytest.mark.asyncio
@respx.mock
async def test_launcher_500_raises_extraction_error():
    respx.post(re.compile(r"http://[^/]+/extract-metadata")).mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(LeanMetadataExtractionError):
        await extract_lean_metadata(
            image_digest="sha256:97884667...",
            launcher_url="http://launcher:8090",
            launcher_token="t",
        )


@pytest.mark.asyncio
@respx.mock
async def test_image_digest_mismatch_raises():
    """Defensive: if the launcher returns a different image digest than we asked
    for (e.g. it pulled latest), refuse the result."""
    respx.post(re.compile(r"http://[^/]+/extract-metadata")).mock(
        return_value=httpx.Response(
            200,
            json={
                "market_hours_database_b64": base64.b64encode(b"x").decode("ascii"),
                "symbol_properties_database_b64": base64.b64encode(b"y").decode("ascii"),
                "image_digest_used": "sha256:deadbeef",
            },
        )
    )
    with pytest.raises(LeanMetadataExtractionError):
        await extract_lean_metadata(
            image_digest="sha256:97884667...",
            launcher_url="http://launcher:8090",
            launcher_token="t",
        )
