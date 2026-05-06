"""Canonical-JSON hashing for run ledger identity fields.

Formula: ``sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8'))``.
Reference: RFC 8785 (JSON Canonicalization Scheme) for the sort-keys + tight separators contract; SHA-256 per FIPS 180-4. Not literally JCS — JCS demands UTF-8 ``\\uXXXX`` escaping for non-ASCII and exact float formatting per ECMAScript ToString. Our use is closed-vocabulary (StrategySpec round-trip + bounded numeric fields) so the simpler contract is sufficient and doesn't pull in a JCS dependency.
Canonical implementation: this file. Used only inside ``app/research/runs/``.
Validated against: ``tests/research/runs/test_hashing.py`` (key-order independence, non-ASCII stability, float-equality, payload nesting).

The hash is a *forward-only* identity device: collisions are not expected
in this domain, and the only consumer is the run ledger's identity
columns. If a run's hash matches an existing run's hash, the run is
treated as a deterministic replay of the same inputs.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(payload: Any) -> str:
    """Serialize ``payload`` to a canonical-JSON string.

    Determinism contract:
      * keys sorted lexicographically at every nesting level
      * no whitespace between separators
      * non-ASCII characters preserved verbatim (UTF-8 round-trip)
      * floats: ``json.dumps`` default (``repr``-style) — sufficient for our
        bounded numeric vocabulary; consumers must not mix Decimal/float
        for the same ledger field

    Pre-conditions: ``payload`` must be JSON-serializable. Pass Pydantic
    models through ``model.model_dump(mode='json')`` first, not
    ``model_dump()`` — the latter leaves ``datetime`` and ``Decimal``
    objects in the tree and ``json.dumps`` will choke.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def hash_payload(payload: Any) -> str:
    """Return ``sha256(canonical_json(payload))`` as a 64-char hex string."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def make_data_snapshot_id(
    *,
    symbol: str,
    resolution_minutes: int,
    start_ms: int,
    end_ms: int,
    data_root_revision: str,
) -> str:
    """Build the ``data_snapshot_id`` ledger field.

    Identity (NOT a content hash of bars — bar-content hashing is
    expensive and unnecessary for this workflow):
      ``f"{symbol}|{resolution_minutes}|{start_ms}|{end_ms}|{data_root_revision}"``

    ``data_root_revision`` is the LEAN-data-root identifier. In v1 the
    runner passes the git HEAD of the data root if it's a git repo, else
    the mtime of the data root directory in seconds, else ``"unknown"``.
    See ``docs/references/run-ledger.md`` for the rationale and the
    upgrade path (content-addressable Parquet snapshots).
    """
    return f"{symbol}|{resolution_minutes}|{start_ms}|{end_ms}|{data_root_revision}"
