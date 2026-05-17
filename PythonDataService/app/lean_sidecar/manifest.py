"""Reproducibility manifest writer.

Per ``docs/architecture/lean-sidecar-lab.md`` §"Reproducibility manifest",
``manifest.json`` records every input that can affect the run's output so
that a normalized result can be replayed (or detected as un-replayable)
later. Anything that changes the manifest hash set invalidates existing
reconciliation fixtures derived from it.

Every timestamp written here is ``int64 ms UTC``. Per
``.claude/rules/numerical-rigor.md`` §"Timestamp rigor", no
``datetime``/``DateTime``/ISO string crosses this boundary.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

# Bump only when the manifest schema changes in a non-additive way. Phase
# 1 fixtures are pinned against schema 1; raising this forces fixtures to
# be re-generated through the golden-fixture lifecycle.
MANIFEST_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """Stream a file into sha256. Used for staged data + config hashes."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Manifest fields
# ---------------------------------------------------------------------------
#
# Field names mirror the ADR's bullet list under §"Reproducibility
# manifest" so that "what is in the manifest" is grep-able against the
# authority doc.


DataAdjustmentPolicy = Literal[
    "raw_with_factor_map_files",
    "pre_adjusted_non_reconciliation",
]

DataNormalizationMode = Literal["Raw", "Adjusted", "TotalReturn"]

BrokeragePolicy = Literal[
    "algorithm_default",
    "interactive_brokers",
]


@dataclass(frozen=True, slots=True)
class StagedDataFile:
    """One staged file with its hash and bytes-on-disk size."""

    path_in_workspace: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class StagedDataManifest:
    """Hashes for every staged file the LEAN container can read."""

    bar_zips: tuple[StagedDataFile, ...] = ()
    factor_files: tuple[StagedDataFile, ...] = ()
    map_files: tuple[StagedDataFile, ...] = ()
    market_hours_database: StagedDataFile | None = None
    symbol_properties_database: StagedDataFile | None = None


@dataclass(frozen=True, slots=True)
class WindowMs:
    """A request/staged/effective window, expressed as int64 ms UTC."""

    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        # Reject swapped or zero-length windows up front; the ADR's
        # alignment check downstream assumes well-formed inputs.
        if self.start_ms >= self.end_ms:
            raise ValueError(f"WindowMs requires start_ms < end_ms; got start={self.start_ms} end={self.end_ms}")


@dataclass(frozen=True, slots=True)
class RunManifest:
    """The full reproducibility manifest. One per run.

    Reordering or removing fields is a schema change. Adding optional
    fields is backwards-compatible; adding mandatory ones requires
    bumping ``MANIFEST_SCHEMA_VERSION`` and rotating existing fixtures.
    """

    schema_version: int
    run_id: str

    # Source
    algorithm_source_sha256: str
    algorithm_type_name: str
    algorithm_language: Literal["Python", "CSharp"]

    # LEAN runtime
    config_json_sha256: str
    lean_image_digest: str
    launcher_version_sha256: str
    normalized_parser_version: str

    # Data
    staged_data: StagedDataManifest
    data_adjustment_policy: DataAdjustmentPolicy
    data_normalization_mode: DataNormalizationMode
    fill_forward: bool

    # Brokerage / account
    brokerage_policy: BrokeragePolicy
    starting_capital: float
    account_currency: str

    # Limits actually applied (echo of RunLimits + caps)
    limits: Mapping[str, Any]
    parameters: Mapping[str, Any]

    # Windows — recorded independently per §"Date-window and bar-consumption"
    requested_window_ms: WindowMs
    staged_data_window_ms: WindowMs | None = None
    effective_algorithm_window_ms: WindowMs | None = None

    # Consumption proof per §"Date-window and bar-consumption"
    bars_consumed_by_symbol: Mapping[str, int] = field(default_factory=dict)

    # Run wall-clock
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    exit_code: int | None = None

    # Diagnostics that survive the parser version bump
    notes: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _as_jsonable(value: Any) -> Any:
    """Recursive dataclass + tuple normalization for json.dumps.

    Tuples become lists; dataclasses become dicts; everything else passes
    through. Datetime objects are forbidden at this boundary — the
    int64-ms-UTC rule applies to every persisted timestamp.
    """
    if hasattr(value, "__dataclass_fields__"):
        return {k: _as_jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, Mapping):
        return {k: _as_jsonable(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_as_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_as_jsonable(v) for v in value]
    if isinstance(value, datetime):
        raise TypeError(
            "Manifest forbids datetime objects; convert to int64 ms UTC "
            "at the ingestion boundary before passing through."
        )
    return value


def write_manifest(manifest: RunManifest, dest: Path) -> Path:
    """Write a manifest as canonical pretty-printed JSON.

    Returns the destination path for chaining. The file is written
    atomically: temp file + rename, so partial writes never appear as a
    valid manifest.
    """
    payload = _as_jsonable(manifest)
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(serialized, encoding="utf-8")
    tmp.replace(dest)
    return dest


def hash_staged_files(root: Path, paths: Iterable[Path]) -> tuple[StagedDataFile, ...]:
    """Hash a collection of files relative to ``root``.

    ``path_in_workspace`` is normalized to forward slashes so the
    manifest is portable across Windows and Linux launcher hosts.
    """
    out: list[StagedDataFile] = []
    for p in paths:
        rel = p.relative_to(root).as_posix()
        out.append(
            StagedDataFile(
                path_in_workspace=rel,
                sha256=sha256_file(p),
                size_bytes=p.stat().st_size,
            )
        )
    return tuple(out)


def now_ms_utc() -> int:
    """Return current wall-clock as int64 ms UTC.

    The only sanctioned way to produce a fresh ms timestamp inside the
    sidecar. Per the rule file, ``datetime.utcnow`` is banned.
    """
    return int(datetime.now(UTC).timestamp() * 1000)
