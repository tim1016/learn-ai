"""Pydantic models, hash helpers, and parquet I/O for prediction-set artifacts.

Reuses ``app.research.runs.hashing.hash_payload`` so all hash strings in
this package are bare 64-char hex, matching ``strategy_spec_hash`` and
``data_snapshot_id`` formats used by the run ledger.

Wire and storage timestamps are ``int64 ms UTC`` per
``.claude/rules/numerical-rigor.md`` -> "Timestamp rigor".
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Path-safe pattern: alphanumerics, underscore, hyphen, dot.
# Disallows leading dot (no hidden files), slashes, traversal.
_PATH_SAFE = re.compile(r"^[A-Za-z0-9_\-][A-Za-z0-9_\-.]*$")


def is_path_safe_id(value: str) -> bool:
    """Return True iff ``value`` is safe to use as a directory name.

    Rejects empty strings, leading dot, anything containing ``/``, ``\\``,
    or ``..``. Used to validate ``prediction_set_id`` before it appears in
    a filesystem path.
    """
    if not value or ".." in value:
        return False
    return bool(_PATH_SAFE.fullmatch(value))


class GeneratorMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["deterministic_rule"]
    rule_id: str
    rule_version: str


class ChunkRef(BaseModel):
    """Reference to one chunk file in the artifact directory.

    Invariants enforced at validation:
      * ``start_ms > trained_through_ms`` (leakage tell)
      * ``end_ms >= start_ms``
      * ``row_count >= 0``
      * ``rows_hash`` is a 64-char hex string
    """

    model_config = ConfigDict(extra="forbid")

    trained_through_ms: int
    start_ms: int
    end_ms: int
    row_count: int = Field(ge=0)
    rows_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _check_invariants(self) -> ChunkRef:
        if self.start_ms <= self.trained_through_ms:
            raise ValueError(
                f"start_ms must be > trained_through_ms "
                f"(got start_ms={self.start_ms}, trained_through_ms={self.trained_through_ms})"
            )
        if self.end_ms < self.start_ms:
            raise ValueError(
                f"end_ms must be >= start_ms (got start_ms={self.start_ms}, end_ms={self.end_ms})"
            )
        return self


class PredictionSetManifest(BaseModel):
    """v0.5 prediction-set manifest. Persisted as ``manifest.json``.

    ``prediction_set_hash`` covers everything in this manifest *except*
    itself (chicken-and-egg): see ``compute_prediction_set_hash``.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    prediction_set_id: str
    symbol: str
    resolution_minutes: int = Field(ge=1)
    field_names: list[str] = Field(min_length=1)
    warmup_policy: Literal["neutral_zero_until_feature_ready"]
    generator: GeneratorMeta
    chunks: list[ChunkRef] = Field(min_length=1)
    prediction_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _check_path_safe_id(self) -> PredictionSetManifest:
        if not is_path_safe_id(self.prediction_set_id):
            raise ValueError(
                f"prediction_set_id must be path-safe "
                f"([A-Za-z0-9_-][A-Za-z0-9_-.]*, no slashes, no traversal); "
                f"got {self.prediction_set_id!r}"
            )
        return self
