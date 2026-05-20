"""Ruff-backed lint endpoint for the LEAN script editor.

The unified Engine Lab (PR B.5, 2026-05-19) lets operators paste a
``MyAlgorithm`` QCAlgorithm source into the in-page editor and submit
the run through the same launch surface as the Python engine. The
editor calls ``POST /api/lean-sidecar/lint`` after each keystroke
(debounced) to surface ruff diagnostics in a Problems panel.

Contract authority: ``docs/superpowers/specs/2026-05-19-pr-b-engine-lab-unified-design.md``
section 6.4. Size cap shared with the trusted-runs endpoint via
``MAX_ALGORITHM_SOURCE_BYTES`` so the lint and submit paths refuse
identical inputs.

Subprocess safety: ruff is spawned with the no-shell variant
``asyncio.subprocess.create_subprocess_exec`` and positional arguments.
The operator-supplied source travels on stdin, never on argv, so a
hostile source string cannot inject command flags or shell
metacharacters.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.lean_sidecar.config import MAX_ALGORITHM_SOURCE_BYTES

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/lean-sidecar", tags=["lean-sidecar"])

_RUFF_TIMEOUT_S = 5.0


class _LintRequest(BaseModel):
    source: str = Field(...)


class _Diagnostic(BaseModel):
    line: int
    col: int
    end_line: int | None = None
    end_col: int | None = None
    rule: str
    severity: str
    message: str
    fix: str | None = None


class _LintResponse(BaseModel):
    diagnostics: list[_Diagnostic]


async def _run_ruff(source_bytes: bytes) -> tuple[bytes, bytes, int]:
    """Spawn ruff with stdin source. No shell. Returns (stdout, stderr, rc)."""
    process = await asyncio.subprocess.create_subprocess_exec(
        "ruff",
        "check",
        "--output-format",
        "json",
        "--stdin-filename",
        "main.py",
        "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(input=source_bytes)
    return stdout, stderr, process.returncode or 0


@router.post("/lint", response_model=_LintResponse)
async def lint_source(payload: _LintRequest) -> _LintResponse:
    """Run ruff against the operator-supplied QCAlgorithm source.

    Behavior contract:
    - Empty / whitespace-only source returns ``{"diagnostics": []}`` without
      spawning ruff (avoids the subprocess hit on every backspace).
    - Source larger than ``MAX_ALGORITHM_SOURCE_BYTES`` is rejected with
      HTTP 413; the editor surfaces the cap to the user.
    - Subprocess that takes longer than ``_RUFF_TIMEOUT_S`` is killed and
      the caller gets HTTP 504. Ruff has a hard limit but a pathological
      regex in user source could conceivably hang.
    - Diagnostics ship as a flat list shaped per spec section 6.4. Ruff's
      exit code 1 simply means "found violations" — we treat it the same
      as exit 0 for transport purposes.
    """
    source_bytes = payload.source.encode("utf-8")
    if len(source_bytes) > MAX_ALGORITHM_SOURCE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "reason": "source_too_large",
                "max_bytes": MAX_ALGORITHM_SOURCE_BYTES,
            },
        )

    if not payload.source.strip():
        return _LintResponse(diagnostics=[])

    try:
        stdout, _stderr, _rc = await asyncio.wait_for(_run_ruff(source_bytes), timeout=_RUFF_TIMEOUT_S)
    except TimeoutError as e:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={"reason": "ruff_timeout", "timeout_seconds": _RUFF_TIMEOUT_S},
        ) from e

    raw = stdout.decode("utf-8").strip()
    if not raw:
        return _LintResponse(diagnostics=[])

    items = json.loads(raw)
    diagnostics = [
        _Diagnostic(
            line=item["location"]["row"],
            col=item["location"]["column"],
            end_line=item.get("end_location", {}).get("row"),
            end_col=item.get("end_location", {}).get("column"),
            rule=item["code"],
            severity="warning",
            message=item["message"],
            fix=(item.get("fix") or {}).get("message"),
        )
        for item in items
    ]
    return _LintResponse(diagnostics=diagnostics)
