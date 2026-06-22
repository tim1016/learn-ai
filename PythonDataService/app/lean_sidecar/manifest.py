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
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

# Bump only when the manifest schema changes in a non-additive way. Phase
# 1 fixtures are pinned against schema 1; raising this forces fixtures to
# be re-generated through the golden-fixture lifecycle.
#
# Version history:
#   1 — Phase 1 baseline. ``requested_window_ms`` was the caller's raw
#       midnight-UTC payload; ``staged_data_window_ms`` was the
#       ET-midnight envelope of staged trading days.
#   2 — P2.5 (2026-05-18): ``requested_window_ms`` now carries the
#       half-open session-boundary contract (start = 09:30 ET of
#       start_date; end = 09:30 ET of next_trading_day(end_date)).
#       Manifest writers add ``date_semantics=session_open_half_open``
#       to ``notes`` so the cross-engine reconciler can branch on
#       which contract a persisted run was written under. Old
#       manifests stay readable on schema_version 1.
#   3 — 2026-05-19: Added ``BarsSpec`` and ``DataPolicyManifest``
#       dataclasses. ``data_policy`` is now a mandatory field on
#       ``RunManifest``, capturing data provenance (source, symbol,
#       adjustment, session, input vs strategy bars, fixture identity)
#       separately from execution-policy fields.
#   4 — 2026-05-19: PR B renamed ``DataPolicyManifest`` to ``DataPolicy``
#       (backend-neutral shared shape) and added the ``provider_kind``
#       field (``"live"`` for live Polygon runs, ``"fixture"`` for
#       replay-driven parity runs). The old class name is retained as a
#       ``DeprecationWarning`` alias in ``app.lean_sidecar.data_policy``.
MANIFEST_SCHEMA_VERSION = 4

# Note tag that the manifest writer adds to ``notes`` so downstream
# readers (the cross-engine reconciler, the run-history sidebar) can
# detect that a manifest was written under the P2.5 contract without
# inspecting the millisecond values.
P2_5_DATE_SEMANTICS_NOTE = "date_semantics=session_open_half_open"


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
class BarsSpec:
    """Polygon-style (timespan, multiplier) pair.

    ``timespan`` matches Polygon's API vocabulary so a reviewer can map
    manifest values directly to /v2/aggs query parameters.
    """

    timespan: Literal["minute", "hour", "day"]
    multiplier: int


@dataclass(frozen=True, slots=True)
class DataPolicy:
    """Where the bars came from and what processing they got.

    Backend-neutral shared shape. Embedded inside ``RunManifest.data_policy``
    for LEAN runs; persisted as JSONB on ``StrategyExecution`` rows for both
    Python and LEAN runs. Renamed from ``DataPolicyManifest`` in PR B
    (2026-05-19); the old name is kept as a deprecation-warning alias in
    ``app.lean_sidecar.data_policy``.

    Separate from the existing top-level ``fill_forward`` /
    ``data_adjustment_policy`` / ``data_normalization_mode`` fields on
    ``RunManifest``, which encode execution-time policy. This block
    encodes data provenance: "what bars did we feed in, and how were
    they constructed?"

    ``provider_kind`` distinguishes live Polygon runs (``"live"``) from
    parity-test runs driven by a recorded fixture (``"fixture"``).
    ``fixture_id`` and ``fixture_sha256`` are populated only when
    ``provider_kind == "fixture"``.
    """

    source: Literal["synthetic", "polygon"]
    symbol: str
    adjusted: bool
    session: Literal["regular", "extended"]
    input_bars: BarsSpec
    strategy_bars: BarsSpec
    timestamp_policy: Literal["bar_close_ms_utc"]
    timezone: Literal["America/New_York"]
    provider_kind: Literal["live", "fixture"]
    fixture_id: str | None
    fixture_sha256: str | None


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
    data_policy: DataPolicy
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

    # Convenience index of every staged zip keyed by workspace-relative
    # path → sha256. Derived from ``staged_data.bar_zips`` but flattened
    # here so a consumer can answer "what did LEAN see at this path?"
    # without traversing the nested tuple. Populated for every run —
    # fixture-backed and live — so a consumer can hash-pin replay
    # without knowing the data source.
    staged_zip_sha256: Mapping[str, str] = field(default_factory=dict)

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
    """Hash a collection of files relative to ``root``, in sorted order.

    ``path_in_workspace`` is normalized to forward slashes so the
    manifest is portable across Windows and Linux launcher hosts.

    Sorting by posix-relative path makes the returned tuple deterministic
    regardless of caller iteration order (filesystem ``rglob``, set
    iteration, etc.). Without this, identical staged inputs could produce
    different manifests and break the reproducibility contract.
    """
    ordered = sorted(paths, key=lambda p: p.relative_to(root).as_posix())
    out: list[StagedDataFile] = []
    for p in ordered:
        rel = p.relative_to(root).as_posix()
        out.append(
            StagedDataFile(
                path_in_workspace=rel,
                sha256=sha256_file(p),
                size_bytes=p.stat().st_size,
            )
        )
    return tuple(out)


from app.utils.timestamps import now_ms_utc as now_ms_utc  # noqa: E402

# Re-exported for back-compat: ``tests/lean_sidecar/test_manifest.py``
# imports ``now_ms_utc`` from this module. Canonical implementation
# lives in ``app/utils/timestamps.py``.
