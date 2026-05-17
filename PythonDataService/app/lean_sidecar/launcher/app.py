"""FastAPI app for the launcher service.

Run with:

    uvicorn app.lean_sidecar.launcher.app:app --host 127.0.0.1 --port 8090

The launcher binds to ``127.0.0.1`` by default; the ADR's Windows fall
back to localhost + shared-secret is honored via the ``LEAN_LAUNCHER_TOKEN``
environment variable (when set, every request must carry the token
header).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, status

from app.lean_sidecar.config import DEFAULT_ARTIFACTS_ROOT
from app.lean_sidecar.launcher.models import LaunchRequest, LaunchResponse
from app.lean_sidecar.launcher.service import LaunchRejectedError, launch

logger = logging.getLogger(__name__)

LAUNCHER_VERSION = "phase-1-spike-0"


def _artifacts_root() -> Path:
    """Resolve the host-side artifacts root from env or default.

    The launcher creates the root if missing (deploys often start with
    an empty volume). It rejects a configured path that already exists
    as a non-directory — a regular file at that path would silently
    cause every later mount + manifest write to fail with confusing
    errors; failing fast here is the boundary check that "validate
    inputs at system boundaries" calls for.
    """
    raw = os.environ.get("LEAN_LAUNCHER_ARTIFACTS_ROOT")
    root = Path(raw).resolve() if raw else DEFAULT_ARTIFACTS_ROOT.resolve()
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise RuntimeError(f"LEAN_LAUNCHER_ARTIFACTS_ROOT must be a directory; got {root}")
    return root


def _expected_token() -> str | None:
    """Return the shared-secret token when set, else None (open localhost)."""
    return os.environ.get("LEAN_LAUNCHER_TOKEN") or None


app = FastAPI(
    title="LEAN Sidecar Launcher",
    description=(
        "Owns Podman API access for the LEAN Sidecar Lab. Receives launch "
        "requests from the data plane; invokes a pinned LEAN container "
        "with the security shape from lean-sidecar-lab.md."
    ),
    version=LAUNCHER_VERSION,
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": LAUNCHER_VERSION}


@app.post("/launch", response_model=LaunchResponse)
async def post_launch(
    request: LaunchRequest,
    x_launcher_token: str | None = Header(default=None, alias="X-Launcher-Token"),
) -> LaunchResponse:
    expected = _expected_token()
    if expected is not None and x_launcher_token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or wrong X-Launcher-Token",
        )
    try:
        return launch(request, artifacts_root=_artifacts_root())
    except LaunchRejectedError as e:
        # 4xx covers all "this request is malformed in a way the
        # launcher refuses to act on". The body carries a stable
        # ``reason`` label so the caller can branch without parsing.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"reason": e.reason, "message": e.detail},
        ) from e
