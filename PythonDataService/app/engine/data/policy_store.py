"""Policy-keyed canonical bar store for both backtest engines.

The shared on-disk LEAN-zip cache is the single canonical bar store:
the Python engine reads it natively (``LeanMinuteDataReader`` /
``LeanDailyDataReader``), the LEAN sidecar stages byte-copies of its
zips into each run workspace, and the ``/api/engine/bars`` endpoint
serves the same bytes to the UI. One write boundary (Polygon →
``polygon_export``), three readers.

Bars fetched with different Polygon parameters produce different bytes
on disk, so the cache is keyed by the DataPolicy dimensions that change
bytes::

    {cache_root}/{source}-{adjusted|raw}/equity/usa/minute/{symbol}/{YYYYMMDD}_trade.zip
    {cache_root}/{source}-{adjusted|raw}/equity/usa/daily/{symbol}.zip
    {cache_root}/{source}-{adjusted|raw}/provenance/{symbol}.json
    {cache_root}/{source}-{adjusted|raw}/locks/{symbol}.lock

``session`` is deliberately NOT part of the key: the store always holds
the full session and both engines filter at read time (see
``LeanMinuteDataReader.session``). Consolidation timeframe is a
downstream concern and never touches the stored bytes.

The pre-policy legacy tree (``{cache_root}/equity/...``) held
adjusted bars with no policy label — the seam bug this module fixes is
that the Python engine cached *adjusted* bars while the LEAN sidecar
fetched *raw* bars, so the two engines never consumed the same bytes on
Polygon-sourced runs. The legacy tree is intentionally no longer read;
data is re-fetched on demand into the policy-keyed roots.

Concurrency: writers must hold :func:`symbol_write_lock` for the
``(policy_root, symbol)`` pair across the check-fetch-write sequence
(see ``availability.ensure_range``). Zip writers are atomic
(write-temp + ``os.replace``) so readers never observe a torn zip.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

PROVENANCE_SCHEMA_VERSION = 1

BarSource = Literal["polygon"]


def resolve_cache_root() -> Path:
    """Return the writable cache root for Polygon-sourced LEAN zips.

    Reads ``LEAN_DATA_CACHE`` if set, otherwise defaults to a sibling
    ``lean-cache`` directory next to the service. Policy roots live
    underneath this directory.
    """
    configured = os.environ.get("LEAN_DATA_CACHE")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[3] / "lean-cache"


def resolve_reference_root() -> Path:
    """Return the read-only LEAN reference Data directory.

    Reads ``LEAN_DATA_ROOT`` if set; otherwise the standard local
    development location. The reference mount is policy-neutral vendored
    ground truth (the bit-exact SPY fixture) and always wins over the
    cache when both cover a date.
    """
    configured = os.environ.get("LEAN_DATA_ROOT")
    if configured:
        return Path(configured)
    return Path("/sessions/ecstatic-hopeful-volta/mnt/Lean/Data")


def policy_key(*, source: BarSource, adjusted: bool) -> str:
    """Derive the cache-subtree key from the byte-changing policy dims."""
    return f"{source}-{'adjusted' if adjusted else 'raw'}"


def resolve_policy_root(
    *,
    source: BarSource,
    adjusted: bool,
    cache_root: Path | None = None,
) -> Path:
    """Return the policy-keyed cache subtree (not created)."""
    root = cache_root if cache_root is not None else resolve_cache_root()
    return root / policy_key(source=source, adjusted=adjusted)


def resolve_data_roots(*, source: BarSource, adjusted: bool) -> list[Path]:
    """Return the ordered reader roots for a policy: [reference?, policy cache].

    Reference mount comes first so the bit-exact SPY fixture always wins
    over anything materialized into the cache for the same date range.
    The policy cache root is created if missing. Both engines and the
    bars endpoint must resolve roots through this single function so
    they always observe the same bytes.
    """
    roots: list[Path] = []
    ref = resolve_reference_root()
    if ref.exists():
        roots.append(ref)
    policy_root = resolve_policy_root(source=source, adjusted=adjusted)
    policy_root.mkdir(parents=True, exist_ok=True)
    roots.append(policy_root)
    return roots


def _safe_symbol(symbol: str) -> str:
    """Validate the symbol before it flows into a filesystem path."""
    # Lazy import: the canonical path-safety validator lives with the
    # sidecar workspace code; the engine layer reuses it rather than
    # duplicating the ticker alphabet (guiding-philosophy #5).
    from app.lean_sidecar.workspace import validate_symbol

    return validate_symbol(symbol)


@contextmanager
def symbol_write_lock(policy_root: Path, symbol: str) -> Iterator[None]:
    """Exclusive advisory lock for cache writes to ``(policy_root, symbol)``.

    Two concurrent runs that ``ensure_range`` the same symbol serialize
    here; the loser of the race re-checks availability under the lock
    and skips its fetch. ``fcntl.flock`` is process- and thread-safe on
    the Linux container filesystems this service runs on.
    """
    safe = _safe_symbol(symbol)
    root_real = os.path.realpath(os.fspath(policy_root))
    root_prefix = root_real.rstrip(os.sep) + os.sep
    lock_candidate = os.path.realpath(os.path.join(root_real, "locks", f"{safe.lower()}.lock"))
    if not lock_candidate.startswith(root_prefix):
        raise ValueError(f"lock path {lock_candidate!r} escapes root {root_real!r}")
    lock_path = Path(lock_candidate)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="ascii") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _provenance_path(policy_root: Path, symbol: str) -> Path:
    return policy_root / "provenance" / f"{_safe_symbol(symbol).lower()}.json"


def read_provenance(policy_root: Path, symbol: str) -> dict | None:
    """Return the symbol's provenance document, or None when absent."""
    root_real = os.path.realpath(os.fspath(policy_root))
    candidate = os.path.realpath(os.fspath(_provenance_path(policy_root, symbol)))
    root_prefix = root_real.rstrip(os.sep) + os.sep
    if not candidate.startswith(root_prefix):
        raise ValueError(f"provenance path {candidate!r} escapes root {root_real!r}")
    path = Path(candidate)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def record_fetch(
    policy_root: Path,
    symbol: str,
    *,
    source: BarSource,
    adjusted: bool,
    resolution: str,
    from_date: str,
    to_date: str,
    fetched_at_ms: int,
) -> Path:
    """Append a fetch record to the symbol's provenance document.

    Caller must hold :func:`symbol_write_lock` for the same
    ``(policy_root, symbol)`` — the read-merge-write below is not atomic
    on its own. Raises ``ValueError`` when an existing document carries
    a different policy than the given root claims: that means a caller
    resolved the wrong policy root, exactly the silent-mismatch class
    this store exists to prevent.
    """
    root_real = os.path.realpath(os.fspath(policy_root))
    candidate = os.path.realpath(os.fspath(_provenance_path(policy_root, symbol)))
    root_prefix = root_real.rstrip(os.sep) + os.sep
    if not candidate.startswith(root_prefix):
        raise ValueError(f"provenance path {candidate!r} escapes root {root_real!r}")
    path = Path(candidate)
    expected_policy = {"source": source, "adjusted": adjusted}
    doc = read_provenance(policy_root, symbol)
    if doc is None:
        doc = {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "symbol": _safe_symbol(symbol),
            "policy": expected_policy,
            "fetches": [],
        }
    elif doc.get("policy") != expected_policy:
        raise ValueError(
            f"provenance policy mismatch under {policy_root}: file says {doc.get('policy')}, caller says {expected_policy}"
        )
    doc["fetches"].append(
        {
            "resolution": resolution,
            "from_date": from_date,
            "to_date": to_date,
            "fetched_at_ms": fetched_at_ms,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path
