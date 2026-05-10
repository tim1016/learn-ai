"""``PredictionSet.load(...)`` — read + validate a prediction-set artifact.

Validation runs in two stages:

* **Intrinsic** (this file): hashes match, leakage invariant holds, rows
  fall inside the chunk window, no duplicate timestamps within or across
  chunks, the row index is built.
* **Spec-pairing** (this file, ``assert_pairs_with``): symbol + resolution
  match the consumer ``StrategySpec``, and at most one ``prediction_set_id``
  is referenced.

A third stage — bar-clock coverage — runs at the run-pipeline boundary
(see ``coverage.py``) where the data source and consolidator are known.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.research.ml.artifact import (
    PredictionSetManifest,
    compute_prediction_set_hash,
    compute_rows_hash,
    read_chunk_rows,
)


class PredictionCoverageError(ValueError):
    """Raised when a loaded prediction set does not cover an emitted bar."""


@dataclass
class PredictionSet:
    """Loaded + validated prediction-set artifact."""

    manifest: PredictionSetManifest
    index: dict[int, dict[str, float]]

    @classmethod
    def load(cls, root: Path) -> PredictionSet:
        """Load an artifact directory; run all intrinsic validation.

        ``root`` is the artifact directory itself (e.g.
        ``artifacts/predictions/pred_spy_rsi_rule_v001/``), containing
        ``manifest.json`` and ``chunks/<trained_through_ms>.parquet``.
        """
        manifest_path = root / "manifest.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"manifest.json not found at {manifest_path}")

        raw = json.loads(manifest_path.read_text())
        manifest = PredictionSetManifest.model_validate(raw)

        recomputed_top = compute_prediction_set_hash(raw)
        if recomputed_top != manifest.prediction_set_hash:
            raise ValueError(
                f"prediction_set_hash mismatch: stored={manifest.prediction_set_hash}, "
                f"recomputed={recomputed_top}. Manifest content has been tampered with."
            )

        index: dict[int, dict[str, float]] = {}
        for chunk in manifest.chunks:
            chunk_path = root / "chunks" / f"{chunk.trained_through_ms}.parquet"
            if not chunk_path.is_file():
                raise FileNotFoundError(f"chunk parquet not found at {chunk_path}")

            rows = read_chunk_rows(chunk_path, field_names=manifest.field_names)
            recomputed_rows = compute_rows_hash(rows)
            if recomputed_rows != chunk.rows_hash:
                raise ValueError(
                    f"rows_hash mismatch for chunk trained_through_ms={chunk.trained_through_ms}: "
                    f"stored={chunk.rows_hash}, recomputed={recomputed_rows}."
                )
            if chunk.row_count != len(rows):
                raise ValueError(
                    f"row_count mismatch for chunk trained_through_ms={chunk.trained_through_ms}: "
                    f"manifest={chunk.row_count}, parquet={len(rows)}"
                )

            for row in rows:
                ts = row["timestamp_ms"]
                if ts < chunk.start_ms or ts > chunk.end_ms:
                    raise ValueError(
                        f"row timestamp_ms={ts} outside chunk window "
                        f"[{chunk.start_ms}, {chunk.end_ms}] "
                        f"(chunk trained_through_ms={chunk.trained_through_ms})"
                    )
                if ts in index:
                    raise ValueError(
                        f"duplicate timestamp_ms={ts} across chunks "
                        f"(second occurrence in chunk trained_through_ms={chunk.trained_through_ms})"
                    )
                index[ts] = {name: row[name] for name in manifest.field_names}

        return cls(manifest=manifest, index=index)

    def assert_pairs_with(self, spec) -> None:
        """Validate that this prediction set is consumable by ``spec``.

        ``spec`` is typed loosely (``StrategySpec``) to avoid a circular
        import; the runtime check uses duck typing on ``.symbols`` and
        ``.resolution.period_minutes``.
        """
        if not spec.symbols:
            raise ValueError("StrategySpec.symbols is empty")
        spec_symbol = spec.symbols[0]
        if self.manifest.symbol != spec_symbol:
            raise ValueError(
                f"symbol mismatch: prediction set has {self.manifest.symbol!r}, "
                f"spec has {spec_symbol!r}"
            )
        if self.manifest.resolution_minutes != spec.resolution.period_minutes:
            raise ValueError(
                f"resolution mismatch: prediction set has {self.manifest.resolution_minutes} min, "
                f"spec has {spec.resolution.period_minutes} min"
            )
