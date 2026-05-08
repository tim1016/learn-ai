"""GET /api/golden-fixtures — catalog of all golden fixtures with validation status.

Reads manifest.json for fixture metadata and artifacts/fixture-validation/latest.json
for the most recent test-run outcome. No live computation at request time (D-010).

Response shape:
  {
    "fixtures": [
      {
        "id": "BS-001",
        "name": "Black-Scholes European Call Price",
        "category": "options-pricing",
        "canonical_module": "...",
        "canonical_callable": "...",
        "reference_kind": "external_reference",
        "is_certified": true,
        "status": "active",
        "active_version": 1,
        "tolerance": {"atol": 1e-10, "rtol": 0.0, "note": "..."}
      },
      ...
    ],
    "validation": {
      "generated_at": "2026-05-09T00:00:00+00:00",
      "passed": 83,
      "failed": 0,
      "errors": 0,
      "status": "ok"
    } | null
  }
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

_SVC_ROOT = Path(__file__).parent.parent.parent
_MANIFEST_PATH = _SVC_ROOT / "tests" / "fixtures" / "golden" / "manifest.json"
_VALIDATION_PATH = _SVC_ROOT / "artifacts" / "fixture-validation" / "latest.json"


class ToleranceResponse(BaseModel):
    atol: float
    rtol: float
    note: str


class FixtureSummary(BaseModel):
    id: str
    name: str
    category: str
    canonical_module: str
    canonical_callable: str
    reference_kind: str
    is_certified: bool
    status: str
    active_version: int
    tolerance: ToleranceResponse


class ValidationSummary(BaseModel):
    generated_at: str
    passed: int
    failed: int
    errors: int
    status: str


class GoldenFixturesCatalog(BaseModel):
    fixtures: list[FixtureSummary]
    validation: ValidationSummary | None = None


_CERTIFIED_KINDS = frozenset(
    {"external_reference", "cross_engine", "literature_formula", "hand_computed"}
)


def _load_manifest() -> list[dict[str, Any]]:
    if not _MANIFEST_PATH.exists():
        raise HTTPException(status_code=503, detail="Golden fixture manifest not found")
    try:
        data = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
        return data.get("fixtures", [])
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read manifest.json: %s", exc)
        raise HTTPException(status_code=503, detail="Golden fixture manifest unreadable") from exc


def _load_validation() -> dict[str, Any] | None:
    if not _VALIDATION_PATH.exists():
        return None
    try:
        return json.loads(_VALIDATION_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read latest.json: %s", exc)
        return None


@router.get("/golden-fixtures", response_model=GoldenFixturesCatalog)
async def get_golden_fixtures() -> GoldenFixturesCatalog:
    """Return the golden fixture catalog with the most recent validation status."""
    raw_fixtures = _load_manifest()
    raw_validation = _load_validation()

    fixtures = []
    for f in raw_fixtures:
        ref = f.get("reference", {})
        tol = f.get("tolerance", {})
        kind = ref.get("kind", "")
        fixtures.append(
            FixtureSummary(
                id=f["id"],
                name=f["name"],
                category=f["category"],
                canonical_module=f["canonical_module"],
                canonical_callable=f["canonical_callable"],
                reference_kind=kind,
                is_certified=kind in _CERTIFIED_KINDS,
                status=f.get("status", "planned"),
                active_version=f.get("active_version", 1),
                tolerance=ToleranceResponse(
                    atol=tol.get("atol", 0.0),
                    rtol=tol.get("rtol", 0.0),
                    note=tol.get("note", ""),
                ),
            )
        )

    validation: ValidationSummary | None = None
    if raw_validation:
        validation = ValidationSummary(
            generated_at=raw_validation.get("generated_at", ""),
            passed=raw_validation.get("passed", 0),
            failed=raw_validation.get("failed", 0),
            errors=raw_validation.get("errors", 0),
            status=raw_validation.get("status", "unknown"),
        )

    return GoldenFixturesCatalog(fixtures=fixtures, validation=validation)
