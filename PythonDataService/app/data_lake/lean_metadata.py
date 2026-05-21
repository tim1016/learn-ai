"""LEAN-image metadata extraction (data-lake-side counterpart).

The polygon-data-service container does not have `podman` on PATH, so it
cannot subprocess-spawn `podman cp` against the LEAN image directly. The
LEAN-sidecar launcher (a host process that DOES have podman) exposes
POST /extract-metadata; this module is the data-lake-side caller.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.5
Existing reference implementation:
  app/lean_sidecar/launcher_client.py — original caller for the lean-sidecar flow
  app/lean_sidecar/launcher/service.py::extract_metadata — launcher endpoint impl

NB: this is intentional duplication of the call path. app/lean_sidecar/ is
retired in Slice 1d; this module is the surviving canonical caller.
"""

from __future__ import annotations

import base64
import logging

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 60.0


class LeanMetadataExtractionError(RuntimeError):
    """Raised when the launcher can't / won't produce the metadata bytes."""


async def extract_lean_metadata(
    image_digest: str,
    launcher_url: str,
    launcher_token: str,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> tuple[bytes, bytes]:
    """Fetch (market_hours_database_bytes, symbol_properties_database_bytes).

    The launcher does the subprocess work; we just transport the bytes. The
    response is base64-encoded JSON to keep the launcher contract a simple
    POST/JSON pair (no multipart, no binary boundary parsing).

    Raises LeanMetadataExtractionError on any failure or digest mismatch.
    """
    url = launcher_url.rstrip("/") + "/extract-metadata"
    headers = {"X-Launcher-Token": launcher_token} if launcher_token else {}
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        try:
            resp = await client.post(
                url,
                json={"image_digest": image_digest},
                headers=headers,
            )
        except httpx.RequestError as e:
            raise LeanMetadataExtractionError(f"launcher unreachable at {url}: {e}") from e

    if resp.status_code != 200:
        raise LeanMetadataExtractionError(f"launcher /extract-metadata returned {resp.status_code}: {resp.text[:200]}")

    payload = resp.json()
    used = payload.get("image_digest_used")
    if used and used != image_digest:
        raise LeanMetadataExtractionError(f"launcher used image_digest={used!r} but {image_digest!r} was requested")

    try:
        mh = base64.b64decode(payload["market_hours_database_b64"])
        sp = base64.b64decode(payload["symbol_properties_database_b64"])
    except (KeyError, ValueError) as e:
        raise LeanMetadataExtractionError(f"launcher returned malformed payload: {e}") from e
    logger.info(
        "data_lake.lean_metadata: extracted %d bytes market-hours + %d bytes symbol-properties for %s",
        len(mh),
        len(sp),
        image_digest,
    )
    return mh, sp
