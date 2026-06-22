"""PRD #619-B — canonical fail-closed reader/writer for Pydantic artifacts.

The live-run + control-plane wire artifacts (``engine_runtime.json``,
``daemon_lease.json``, ``verdict_snapshot.json``, ``run_status.json``,
``mutation_attempt`` records) all share the same read semantics:

1. Missing file → ``None``.
2. ``OSError`` on read → ``None``.
3. JSON decode / Pydantic validation failure → ``None``.
4. ``schema_version`` ahead of this writer's known version → ``None``.

Without a shared helper each artifact reader re-implements the four
guards, and the fourth (forward-incompatible ``schema_version``) is
subtle enough to drift between sites. This module centralises the
contract: every reader is one ``read_pydantic_artifact(path, Model)``
call.

The reader is intentionally narrow — it does not classify *why* the
read failed, log, or raise. The caller's domain logic decides what
``None`` means in context (Resume gate UNKNOWN, freshness UNKNOWN,
orphan classification NO_SIDECAR, etc.).

The matching ``atomic_write_pydantic_artifact`` centralises the
``tmp + fsync + replace`` pattern every writer needs: a partial reader
must never observe a torn file. Direct write callers (no model
involved) can use ``atomic_write_bytes`` instead.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel


def read_pydantic_artifact[T: BaseModel](path: Path, model: type[T]) -> T | None:
    """Read a JSON artifact and parse it through ``model``.

    Returns ``None`` if any of the four failure modes above is hit;
    otherwise returns the validated instance.

    Schema-version policy: when ``model`` has a ``schema_version``
    field with a defined default, a deserialized value greater than
    that default reads as ``None``. This is the fail-closed forward-
    incompatibility contract — a reader that does not understand a
    newer schema must surface "unknown" rather than parse a partial
    subset of a future contract.
    """
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        artifact = model.model_validate_json(text)
    except ValueError:
        return None
    if not _schema_version_compatible(artifact, model):
        return None
    return artifact


def atomic_write_pydantic_artifact(path: Path, artifact: BaseModel) -> None:
    """Atomically persist ``artifact`` to ``path`` as canonical JSON.

    Writes a sibling ``<path>.tmp``, fsyncs, then ``replace``s onto
    ``path`` in one POSIX rename. A concurrent reader observes either
    the prior contents or the new file, never a torn intermediate
    state. The parent directory is created if absent so callers don't
    need to mkdir defensively.

    JSON is emitted with sorted keys and no extra whitespace so the
    on-disk bytes are stable for any downstream content-hash or diff
    comparison.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = artifact.model_dump(mode="json")
    data = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)


def _schema_version_compatible(artifact: BaseModel, model: type[BaseModel]) -> bool:
    """True iff ``artifact.schema_version`` is at or below the writer's
    known default. Models without a ``schema_version`` field are
    treated as always-compatible (no contract version to compare)."""
    field = model.model_fields.get("schema_version")
    if field is None:
        return True
    default = field.default
    if default is None:
        return True
    actual = getattr(artifact, "schema_version", None)
    if actual is None:
        return True
    return actual <= default
