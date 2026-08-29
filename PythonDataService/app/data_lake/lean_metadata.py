"""LEAN-image metadata extraction (data-lake-side counterpart).

The polygon-data-service container does not have `podman` on PATH, so it
cannot subprocess-spawn `podman cp` against the LEAN image directly. The
LEAN-sidecar launcher (a host process that DOES have podman) exposes
POST /extract-metadata; this module is the data-lake-side caller.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.5
Existing reference implementation:
  app/lean_sidecar/launcher_client.py — original caller for the lean-sidecar flow
  app/lean_sidecar/staging.py::_stage_lean_metadata_via_launcher — how that
    caller actually consumes the response (below)
  app/lean_sidecar/launcher/service.py::extract_metadata — launcher endpoint impl

NB: this is intentional duplication of the call path. app/lean_sidecar/ is
retired in Slice 1d; this module is the surviving canonical caller.

The launcher's response carries the paths it wrote *as it sees them* on the
launcher host — not portable to this container's view, and
``ExtractMetadataResponse``'s own docstring says the data plane should not
use them. The actual contract is: the launcher writes files into the run's
workspace under the shared artifacts bind mount, and the caller re-resolves
that same workspace locally (``resolve_workspace`` + ``list_metadata_databases``,
the same primitives the launcher used server-side) and reads the bytes off
its own view of the identical mount. This mirrors
``_stage_lean_metadata_via_launcher`` exactly; a prior version of this
function instead expected a base64-encoded-bytes response the launcher has
never sent.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from app.lean_sidecar import config as sidecar_config
from app.lean_sidecar.staging import list_metadata_databases
from app.lean_sidecar.workspace import resolve_workspace

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 60.0


class LeanMetadataExtractionError(RuntimeError):
    """Raised when the launcher can't / won't produce the metadata bytes."""


async def extract_lean_metadata(
    image_digest: str,
    launcher_url: str,
    launcher_token: str,
    run_id: str,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    artifacts_root: Path | None = None,
) -> tuple[bytes, bytes]:
    """Fetch (market_hours_database_bytes, symbol_properties_database_bytes).

    The launcher does the subprocess work and writes the two files into the
    ``run_id`` workspace under the shared artifacts bind mount; this function
    then reads them back through this container's own view of that mount
    (see module docstring for why the HTTP response body isn't the transport).

    ``run_id`` names that workspace (``ExtractMetadataRequest.run_id`` in
    app.lean_sidecar.launcher.models, validated there against
    ``^[a-z0-9][a-z0-9_-]{2,63}$``) — the launcher has no notion of a
    bootstrap call with no run behind it, so the caller must always supply
    one. A lowercase UUID (optionally prefixed) satisfies that pattern.

    ``artifacts_root`` overrides the default artifacts root; tests use this
    to point at a tmp_path instead of the real shared mount. Production
    callers leave it at its default.

    Raises LeanMetadataExtractionError on any failure.
    """
    url = launcher_url.rstrip("/") + "/extract-metadata"
    headers = {"X-Launcher-Token": launcher_token} if launcher_token else {}
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        try:
            resp = await client.post(
                url,
                json={"run_id": run_id, "image_digest": image_digest},
                headers=headers,
            )
        except httpx.RequestError as e:
            raise LeanMetadataExtractionError(f"launcher unreachable at {url}: {e}") from e

    if resp.status_code != 200:
        raise LeanMetadataExtractionError(f"launcher /extract-metadata returned {resp.status_code}: {resp.text[:200]}")

    root = artifacts_root if artifacts_root is not None else sidecar_config.DEFAULT_ARTIFACTS_ROOT
    workspace = resolve_workspace(run_id, root)
    mh_path, sp_path = list_metadata_databases(workspace)
    if mh_path is None or sp_path is None:
        raise LeanMetadataExtractionError(
            f"launcher reported success but the workspace is missing the extracted "
            f"databases; market-hours={mh_path!r}, symbol-properties={sp_path!r}"
        )
    mh = mh_path.read_bytes()
    sp = sp_path.read_bytes()
    logger.info(
        "data_lake.lean_metadata: extracted %d bytes market-hours + %d bytes symbol-properties for %s",
        len(mh),
        len(sp),
        image_digest,
    )
    return mh, sp
