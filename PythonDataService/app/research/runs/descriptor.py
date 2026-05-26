"""Artifact descriptor for the runs/ phase.

Declared at module level (decision 6 of the seam doc) so the phase's
on-disk identity is grep-able from one place. Consumed by
``app/research/runs/storage.py`` to construct an ``ArtifactStore``
at each call site.

Runs/-specific twists vs the other three migrated phases:

  * ``subdir=""`` — the runs phase uses a flat layout. Artifacts live
    at ``<root>/<run_id>/``, not ``<root>/runs/<run_id>/``. The
    descriptor's ``subdir: str`` field accepts the empty string;
    coverage is encoded in
    ``tests/research/artifact/test_store.py::test_store_with_empty_subdir_writes_artifact_at_root_directly``
    so reviewers see the shape is intentional, not missing.
  * ``config_filename="ledger.json"`` — not ``"config.json"``. The
    ledger is the *immutable identity object* per
    ``app/engine/live/run_ledger.py``; renaming would erase a
    semantic distinction the codebase already encodes (seam doc
    decision 1).
  * ``hash_payload`` wired up for the first time — see the seam
    doc decision 2. The runner is currently the canonical place
    where the ledger's identity hashes (``result_hash``,
    ``trade_log_hash``, ``metrics_hash``) get attached, so this
    callback is a side-effect hook over the already-hashed ledger
    rather than the producer of the hash bytes. Future use cases
    (parity-test invariant assertions, dual-write to a Postgres
    index) plug in here without changing ``runs/hashing.py``,
    which remains the canonical SHA-256-over-canonical-JSON
    implementation per ``docs/references/run-ledger.md``.
"""

from __future__ import annotations

import re

from app.research.artifact.descriptor import ArtifactDescriptor
from app.research.runs.errors import (
    RunAlreadyExistsError,
    RunCorruptError,
    RunNotFoundError,
)
from app.research.runs.hashing import hash_payload as _hash_canonical
from app.research.runs.ledger import RunLedger

# Strict whitelist for ``run_id`` — exactly what ``uuid.uuid4().hex``
# emits: 32 lowercase hex chars, no separators. Same alphabet the
# other migrated phases use for their ids; see
# ``monte_carlo/descriptor.py`` for the rationale on why hyphens /
# uppercase are not admitted.
_RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _hash_ledger(ledger: RunLedger) -> str:
    """Compute the canonical-JSON SHA-256 of a ``RunLedger``.

    Wraps the canonical implementation in ``runs/hashing.py`` — that
    function stays the single source of truth for the
    canonical-JSON encoding per ``docs/references/run-ledger.md``;
    this wrapper just adapts the ``(BaseModel) -> str`` shape the
    descriptor expects to the ``(dict) -> str`` shape
    ``hash_payload`` accepts. ``model_dump(mode='json')`` is the
    same dictification the pre-seam runner used to feed the hash
    function, so the resulting bytes are bit-identical.
    """
    return _hash_canonical(ledger.model_dump(mode="json"))


RUNS_ARTIFACT = ArtifactDescriptor(
    subdir="",
    id_field="run_id",
    id_pattern=_RUN_ID_PATTERN,
    config_filename="ledger.json",
    result_filename="result.json",
    parent_run_id_extractor=lambda cfg: getattr(cfg, "parent_run_id", None),
    log_tag="RUNS",
    hash_payload=_hash_ledger,
    not_found_error=RunNotFoundError,
    already_exists_error=RunAlreadyExistsError,
    corrupt_error=RunCorruptError,
)
"""On-disk identity of the runs/ phase, consumed by ``ArtifactStore``."""


__all__ = ["RUNS_ARTIFACT", "RunLedger"]
