"""Cell manifest schema + sha256 helpers.

Schema is `schema_version=1`. Any non-additive change MUST bump
schema_version and document the migration in the cell's attribution.md.

Reference: docs/superpowers/specs/2026-05-21-cross-engine-golden-matrix-design.md
            § "Cell manifest.json schema (v1)"
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 1
_CHUNK = 1 << 16


def sha256_of_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class WindowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Literal["W6mo", "W12mo", "W24mo"]
    start_date: str
    end_date: str
    session: Literal["regular", "extended"]
    trading_days_expected: int = Field(..., gt=0)


class StrategySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trusted_sample: str
    trusted_sample_source_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    parameters_constants: dict[str, int | float]
    runtime_parameters: dict[str, str | int | float]


class DataSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lean_data_capture_ref: str
    data_contract_hash: str = Field(..., pattern=r"^[a-f0-9]{64}$")


class BrokerSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    brokerage_model: str
    account_type: str
    fill_model: str
    fee_model: str


class LeanRuntimeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Accepts either the upstream-pinned form (older parity matrices
    # captured before the Phase 1c sandbox derivative) or the current
    # local derivative ``learn-ai/lean-sandbox`` (see ``config.py``
    # and ``PythonDataService/lean_sidecar/Dockerfile``).
    container_image_digest: str = Field(
        ...,
        pattern=r"^(?:docker\.io/quantconnect/lean|localhost/learn-ai/lean-sandbox)@sha256:[a-f0-9]{64}$",
    )


class PinnedArtifactHashes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    orders_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    state_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    observations_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    reconciliation_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")


class StateCsvSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    columns: list[str]
    column_types: dict[str, str]

    @model_validator(mode="after")
    def columns_match_column_types(self) -> StateCsvSchema:
        if set(self.columns) != set(self.column_types.keys()):
            raise ValueError("state_csv_schema.columns must match column_types keys exactly")
        return self


_CELL_ID_RE = re.compile(r"[A-Z]+_W(6|12|24)mo_\d{4}-\d{2}-\d{2}_to_\d{4}-\d{2}-\d{2}")


class CellManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    cell_id: str
    ticker: str
    window: WindowSpec
    strategy: StrategySpec
    data: DataSpec
    broker: BrokerSpec
    lean_runtime: LeanRuntimeSpec
    artifacts: PinnedArtifactHashes
    state_csv_schema: StateCsvSchema
    timezone: Literal["America/New_York"]
    timestamp_convention: Literal["int64_ms_utc"]
    fixture_git_commit: str = Field(..., pattern=r"^[a-f0-9]{7,40}$")
    python_data_service_commit: str = Field(..., pattern=r"^[a-f0-9]{7,40}$")
    generator_script_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    captured_by: str
    captured_at_ms_utc: int = Field(..., gt=0)

    @field_validator("cell_id")
    @classmethod
    def cell_id_format(cls, v: str) -> str:
        if not _CELL_ID_RE.fullmatch(v):
            raise ValueError(f"cell_id has wrong shape: {v!r}")
        return v
