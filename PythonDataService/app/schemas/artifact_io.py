"""PRD #619-B — canonical fail-closed reader for Pydantic JSON artifacts.

The live-run + control-plane wire artifacts (``engine_runtime.json``,
``daemon_lease.json``, ``verdict_snapshot.json``, ``run_status.json``)
all share the same read semantics:

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
"""

from __future__ import annotations

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
