"""File-backed artifact store shared across research phases.

Owns the persistence mechanics — path construction, id validation,
atomic tmp+rename writes, load-and-validate-into-provided-Pydantic-
types, list/filter, the optional hash hook, and the parent-run-id
extractor — that every phase under ``app/research/`` would otherwise
duplicate. Modeled on ``app/research/runs/storage.py`` (the most
thorough of the four pre-seam storage modules); see
``docs/architecture/research-artifact-seam.md`` for the design.

The default artifacts root resolves via
``app.research.artifact.root.default_artifacts_root`` so the
``LEARN_AI_ARTIFACTS_ROOT`` env var keeps working unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from app.research.artifact.descriptor import ArtifactDescriptor
from app.research.artifact.root import default_artifacts_root

logger = logging.getLogger(__name__)

C = TypeVar("C", bound=BaseModel)
R = TypeVar("R", bound=BaseModel)


def _atomic_write_json(path: Path, payload: dict | list) -> None:
    """Write ``payload`` to ``path`` atomically (tmp + ``os.replace``).

    Atomic on POSIX and Windows so a reader either sees the previous
    contents or the new contents — never a half-written file. The temp
    file gets a unique name via ``tempfile.mkstemp`` so two concurrent
    writes to the same target don't stomp each other's temp file
    before the rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


class ArtifactStore:
    """Persistence mechanics for one research phase.

    Bound at construction to an ``ArtifactDescriptor`` (decision 6 in
    the seam doc) so the phase's on-disk identity is grep-able from
    one place. Dependency-injectable for tests via the ``root`` kwarg.
    """

    def __init__(self, descriptor: ArtifactDescriptor, *, root: Path | None = None):
        self._descriptor = descriptor
        self._root_override = root

    # ---- internals -------------------------------------------------

    def _base(self) -> Path:
        """Return ``<root>/<subdir>`` — the directory containing artifact ids."""
        root = self._root_override if self._root_override is not None else default_artifacts_root()
        return root / self._descriptor.subdir

    def _artifact_dir(self, artifact_id: str) -> Path:
        """Resolve an artifact directory, refusing anything that escapes the base.

        Defense in depth against path traversal: the format check
        rejects ``../`` segments and absolute paths; the resolved-path
        check catches anything that slips past (e.g. symlinked roots,
        weird Windows separators). Artifact ids reach here from
        user-controlled URL path segments, so neither layer is
        optional.
        """
        pattern = self._descriptor.id_pattern
        if not artifact_id or not pattern.fullmatch(artifact_id):
            raise ValueError(
                f"artifact_id must match {pattern.pattern} (got {artifact_id!r})"
            )
        base = self._base()
        candidate = (base / artifact_id).resolve()
        base_resolved = base.resolve()
        if not candidate.is_relative_to(base_resolved):
            raise ValueError(
                f"artifact_id resolves outside the artifacts root: {artifact_id!r}"
            )
        return base / artifact_id

    # ---- public surface --------------------------------------------

    def save(
        self,
        config: BaseModel,
        result: BaseModel,
        *,
        replace: bool = False,
    ) -> Path:
        """Persist ``(config, result)`` and return the artifact directory.

        Extracts the artifact id from the config via the descriptor's
        ``id_pattern`` — the convention is that the id field is named
        ``<subdir-singular>_id`` (e.g. ``monte_carlo_id``), but the
        store does not enforce that name; callers supply a config
        whose id round-trips through ``id_pattern.match``. Identity
        is taken from the config; result's identity is the caller's
        responsibility to keep in sync (the thin per-phase delegator
        is the right place for that cross-check).
        """
        # Pull the id from the descriptor's named field. Explicit
        # rather than auto-scanned because some configs carry more
        # than one id-shaped field — e.g. ``MonteCarloConfig`` has
        # both ``monte_carlo_id`` (the artifact's identity) and
        # ``parent_run_id`` (a foreign key into ``runs/``). Letting
        # the store guess by regex match was a real bug: any MC
        # save where the two ids happened to share the ``[0-9a-f]{32}``
        # shape raised ``multiple distinct id-shaped fields ...``
        # before writing anything to disk.
        artifact_id = _get_id(config, self._descriptor)

        artifact_dir = self._artifact_dir(artifact_id)
        if artifact_dir.exists() and not replace:
            raise self._descriptor.already_exists_error(
                f"artifact directory already exists: {artifact_dir} "
                f"(pass replace=True to clobber)"
            )

        # Optional canonical-JSON hash hook (decision 2). Invoke when
        # present so phases that opt in (PR 4: ``runs``) get their
        # hash computed at save time without the store needing to
        # know how to hash any particular phase's config.
        hash_callback = self._descriptor.hash_payload
        if hash_callback is not None:
            hash_callback(config)

        # Write order matters — write the *result* first so a crash
        # between writes leaves an invisible orphan rather than a
        # complete-looking dir with a stale result. Matches the
        # convention documented in ``runs/storage.save_run``.
        _atomic_write_json(
            artifact_dir / self._descriptor.result_filename,
            result.model_dump(mode="json"),
        )
        _atomic_write_json(
            artifact_dir / self._descriptor.config_filename,
            config.model_dump(mode="json"),
        )
        return artifact_dir

    def load(
        self,
        artifact_id: str,
        *,
        config_type: type[C],
        result_type: type[R],
    ) -> tuple[C, R]:
        """Load a previously-saved artifact by id.

        Raises:
          * ``descriptor.not_found_error`` when the artifact directory
            or either of its files is missing.
          * ``descriptor.corrupt_error`` when JSON parses but Pydantic
            validation fails — typically a schema mismatch we owe a
            migration for.
        """
        artifact_dir = self._artifact_dir(artifact_id)
        config_path = artifact_dir / self._descriptor.config_filename
        result_path = artifact_dir / self._descriptor.result_filename

        if not config_path.is_file() or not result_path.is_file():
            raise self._descriptor.not_found_error(
                f"artifact not found: {artifact_id} (looked in {artifact_dir})"
            )

        # Parse each file separately so the error message names the
        # one that's actually corrupt — important after a partial-
        # write recovery has left only one file readable.
        try:
            config = config_type.model_validate_json(
                config_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise self._descriptor.corrupt_error(
                f"failed to parse {config_path}: {exc}"
            ) from exc
        try:
            result = result_type.model_validate_json(
                result_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            raise self._descriptor.corrupt_error(
                f"failed to parse {result_path}: {exc}"
            ) from exc

        return config, result

    def list_ids(
        self,
        *,
        parent_run_id: str | None = None,
        since_ms: int | None = None,
        limit: int | None = None,
    ) -> list[str]:
        """Enumerate persisted artifact ids, optionally filtered.

        Filters are AND-combined. ``since_ms`` compares against the
        config's ``created_at_ms`` field; ``parent_run_id`` compares
        against whatever the descriptor's
        ``parent_run_id_extractor`` returns. Results are sorted by
        ``created_at_ms`` descending so the newest artifacts appear
        first; ``limit`` truncates after sorting.

        Corrupt configs are *skipped* with a warning rather than
        raising — a single broken artifact should not blind the rest
        of the listing. Use ``load`` directly when you need the
        failure to be loud. Directories whose name fails the id
        pattern are also skipped with a warning so the listing's
        return values are always loadable: a caller running
        ``for id in list_ids(): load(id)`` should never have ``load``
        reject an id that ``list_ids`` just returned.
        """
        base = self._base()
        if not base.is_dir():
            return []

        # ``list_ids`` parses each config so it can sort by
        # ``created_at_ms`` and run the descriptor's parent-id
        # extractor. The pre-seam ``list_*`` functions did the same
        # scan-and-parse; phase-specific delegators that need the
        # parsed configs anyway can reuse this work by parsing again
        # (PR-1 baseline) or by extending the store API in a future
        # PR if profiling shows the double-parse matters.
        id_pattern = self._descriptor.id_pattern
        # When ``subdir`` is empty, the base IS the shared artifacts
        # root — so the iterdir below sees sibling phase subdirs
        # (``monte-carlo/``, ``walk-forward/``, ``baselines/``)
        # alongside our own artifact ids. Warning on every sibling
        # would spam ``[RUNS] skipping corrupt ...`` on every healthy
        # listing call. Inside a phase-specific subdir, mismatched
        # names ARE debris worth flagging; at the shared root they're
        # expected coexistence.
        warn_on_name_mismatch = self._descriptor.subdir != ""
        out: list[tuple[str, int]] = []
        for child in base.iterdir():
            if not child.is_dir():
                continue
            # Pre-filter by ``id_pattern`` so list_ids never returns a
            # name that ``load`` would immediately reject. A debris
            # directory inside a phase-specific subdir (manual debug,
            # partial recovery, accidental mkdir) gets a warning
            # rather than silent skip so operators see it in logs;
            # the warning carries the ``skipping corrupt`` phrase to
            # match the same operator grep pattern the corrupt-config
            # path below uses.
            if not id_pattern.fullmatch(child.name):
                if warn_on_name_mismatch:
                    logger.warning(
                        "[%s] skipping corrupt artifact dir at %s "
                        "(name does not match id_pattern %s)",
                        self._descriptor.log_tag,
                        child,
                        id_pattern.pattern,
                    )
                continue
            config_path = child / self._descriptor.config_filename
            if not config_path.is_file():
                continue
            try:
                payload = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning(
                    "[%s] skipping corrupt config at %s: %s",
                    self._descriptor.log_tag,
                    config_path,
                    exc,
                )
                continue

            if not isinstance(payload, dict):
                logger.warning(
                    "[%s] skipping non-object config at %s",
                    self._descriptor.log_tag,
                    config_path,
                )
                continue

            created_at_ms = payload.get("created_at_ms")
            if not isinstance(created_at_ms, int):
                # No usable timestamp — skip rather than raise; same
                # tolerance as a corrupt config.
                logger.warning(
                    "[%s] skipping config without int created_at_ms at %s",
                    self._descriptor.log_tag,
                    config_path,
                )
                continue

            if since_ms is not None and created_at_ms < since_ms:
                continue

            if parent_run_id is not None:
                # Run the descriptor's extractor on a *parsed* model
                # so the callback sees structured access (and so
                # field renames in the config force a coherent
                # descriptor update rather than silently breaking
                # the dict lookup).
                try:
                    # We can't construct the Pydantic model here
                    # without knowing its type — defer to the
                    # extractor working over the raw payload. The
                    # extractor in practice is a tiny lambda like
                    # ``lambda cfg: cfg.parent_run_id``, so wrap
                    # the payload in a lightweight namespace.
                    extracted = self._descriptor.parent_run_id_extractor(
                        _PayloadView(payload)  # type: ignore[arg-type]
                    )
                except Exception as exc:
                    logger.warning(
                        "[%s] parent_run_id_extractor failed at %s: %s",
                        self._descriptor.log_tag,
                        config_path,
                        exc,
                    )
                    continue
                if extracted != parent_run_id:
                    continue

            out.append((child.name, created_at_ms))

        out.sort(key=lambda pair: pair[1], reverse=True)
        if limit is not None:
            out = out[:limit]
        return [artifact_id for artifact_id, _ in out]


def _get_id(config: BaseModel, descriptor: ArtifactDescriptor) -> str:
    """Pull and validate the artifact id from the descriptor's named field.

    Reads ``getattr(config, descriptor.id_field)`` and checks it
    against ``descriptor.id_pattern``. Raises ``ValueError`` with the
    same shape the path-traversal guard uses when the value is
    missing, not a string, or fails the regex — so save's failure
    mode is consistent regardless of whether the bad id came from the
    config (here) or a URL path segment (``_artifact_dir``).
    """
    pattern = descriptor.id_pattern
    value = getattr(config, descriptor.id_field, None)
    if not isinstance(value, str):
        raise ValueError(
            f"config {type(config).__name__}.{descriptor.id_field} must be a "
            f"string id matching {pattern.pattern} (got {value!r})"
        )
    if not pattern.fullmatch(value):
        raise ValueError(
            f"config {type(config).__name__}.{descriptor.id_field} must match "
            f"{pattern.pattern} (got {value!r})"
        )
    return value


class _PayloadView:
    """Attribute-style view over a JSON-decoded dict.

    Allows the descriptor's ``parent_run_id_extractor`` — written as
    ``lambda cfg: cfg.parent_run_id`` on the *Pydantic* model — to
    also work over the raw JSON payload during ``list_ids``, without
    paying the cost of constructing the full Pydantic instance for
    every artifact in the directory.
    """

    __slots__ = ("_payload",)

    def __init__(self, payload: dict):
        self._payload = payload

    def __getattr__(self, name: str) -> object:
        try:
            return self._payload[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
