"""Pydantic schema for tests/fixtures/golden/manifest.json.

The manifest is the single source of truth for the golden fixture system:
- which fixtures exist
- which version of each is active
- where each fixture's files live
- what tolerance applies and why
- what reference_kind certifies the fixture

JSON Schema is derived from these models via model_json_schema() and
committed to tests/fixtures/golden/manifest.schema.json so CI can
validate the manifest without importing Pydantic.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, field_validator, model_validator

MANIFEST_PATH = Path(__file__).parent.parent / "golden" / "manifest.json"
SCHEMA_PATH = Path(__file__).parent.parent / "golden" / "manifest.schema.json"

# reference_kinds that earn "externally certified" status in the UI
CERTIFIED_KINDS: frozenset[str] = frozenset(
    {"external_reference", "cross_engine", "literature_formula", "hand_computed"}
)


class Tolerance(BaseModel):
    atol: float
    rtol: float
    note: str  # required — explains why this tolerance and not the default


class MarketInput(BaseModel):
    source: Literal["synthetic", "polygon", "massive", "vendored"]
    vendor: Optional[str] = None
    generated_by: Optional[str] = None  # script path if synthetic


class Reference(BaseModel):
    kind: Literal[
        "external_reference",
        "cross_engine",
        "literature_formula",
        "vendor_observed",
        "hand_computed",
        "internal_regression",
    ]
    oracle: str
    citation: str

    @property
    def is_certified(self) -> bool:
        return self.kind in CERTIFIED_KINDS


class Units(BaseModel):
    theta: Optional[Literal["per_day", "per_year"]] = None
    vega: Optional[Literal["per_vol_point", "per_one_vol"]] = None
    rho: Optional[Literal["per_rate_point", "per_bp", "per_one_rate"]] = None


class FixtureFiles(BaseModel):
    input: str
    output: str
    attribution: str
    content_sha256: dict[str, str]  # filename -> hash
    file_sha256: dict[str, str]  # filename -> hash

    @field_validator("content_sha256", "file_sha256")
    @classmethod
    def _hashes_are_hex(cls, v: dict[str, str]) -> dict[str, str]:
        for filename, h in v.items():
            if len(h) != 64 or not all(c in "0123456789abcdef" for c in h):
                raise ValueError(f"{filename}: expected 64-char lowercase hex SHA-256, got {h!r}")
        return v


class Fixture(BaseModel):
    id: str
    name: str
    category: str
    canonical_module: str
    canonical_callable: str
    reference: Reference
    market_input: MarketInput
    units: Optional[Units] = None
    tolerance: Tolerance
    active_version: int
    versions: dict[int, FixtureFiles]
    status: Literal["planned", "active", "breach", "deprecated"]

    @model_validator(mode="after")
    def _active_version_exists(self) -> "Fixture":
        if self.status != "planned" and self.active_version not in self.versions:
            raise ValueError(
                f"Fixture {self.id!r}: active_version={self.active_version} "
                f"not in versions={list(self.versions)}"
            )
        return self

    @property
    def active_files(self) -> Optional[FixtureFiles]:
        return self.versions.get(self.active_version)


class Manifest(BaseModel):
    schema_version: int = 1
    fixtures: list[Fixture]

    def by_id(self, fixture_id: str) -> Optional[Fixture]:
        for f in self.fixtures:
            if f.id == fixture_id:
                return f
        return None

    @classmethod
    def load(cls, path: Path = MANIFEST_PATH) -> "Manifest":
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path = MANIFEST_PATH) -> None:
        path.write_text(
            self.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def generate_json_schema(cls) -> dict:
        return cls.model_json_schema()


def write_json_schema(path: Path = SCHEMA_PATH) -> None:
    schema = Manifest.generate_json_schema()
    path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
