"""Shared identity validation for live-runtime path builders.

``strategy_instance_id`` keys an on-disk directory under
``<artifacts_root>/live_state/`` for three sibling sidecars
(``desired_state.json``, ``live_state.json``, and the indicator-state
sidecar). Operator input reaches all three via ``run.py`` (the required
``--strategy`` / ``--strategy-instance-id`` CLI arguments). This module
holds the single validator they share so the path boundary fails closed
the same way everywhere, and lives in its own module to avoid a circular
import between ``desired_state`` and ``live_state_sidecar``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Canonical single-segment id pattern. Kept byte-identical to
# ``live_instances._INSTANCE_ID_RE`` (the operate-endpoint guard that keeps the
# value off the CodeQL path-injection taint chain); a parity test pins the two
# in lockstep. Enforcing it here is what makes a name that ``status``/``start``/
# ``stop`` would later reject (e.g. one containing a space) fail closed at
# *creation* time — via the CLI and the deploy seam — instead of producing an
# instance that exists but can never be operated on.
_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def validate_strategy_instance_id(value: str) -> str:
    """Reject a ``strategy_instance_id`` that is unsafe as a path segment.

    A value containing a path separator (``/`` or ``\\``), a ``..``/``.``
    traversal segment, surrounding whitespace, or a NUL byte could escape
    ``artifacts_root`` or bind a run to the wrong control files. We fail
    closed at the path boundary rather than trust the caller. Returns the
    value unchanged when it is a single safe path segment.

    On the empty string: UI-0 made ``strategy_instance_id`` default to
    ``""`` on ``LiveEngine``, but that default never reaches a path
    builder — the engine only persists desired-state through the
    ``desired_state_writer`` callable wired up in ``run.py``, where the
    id always comes from the (required) ``--strategy`` CLI argument. An
    empty id WOULD yield ``live_state//<file>`` (an empty directory
    segment), so we reject it here too.
    """
    if value != value.strip():
        raise ValueError(
            f"strategy_instance_id must not have leading/trailing whitespace: {value!r}"
        )
    if value == "":
        raise ValueError("strategy_instance_id must not be empty")
    if "\x00" in value:
        raise ValueError(f"strategy_instance_id must not contain a NUL byte: {value!r}")
    if "/" in value or "\\" in value:
        raise ValueError(
            f"strategy_instance_id must not contain a path separator: {value!r}"
        )
    if value in ("..", "."):
        raise ValueError(
            f"strategy_instance_id must not be a path-traversal segment: {value!r}"
        )
    if _INSTANCE_ID_RE.fullmatch(value) is None:
        raise ValueError(
            "strategy_instance_id must be 1-128 chars, start with a letter or "
            "digit, and contain only letters, digits, '_', '.', or '-' "
            f"(no spaces): {value!r}"
        )
    return value


def safe_strategy_instance_path_segment(value: str) -> str:
    """Return a regex-captured, single-segment strategy instance id.

    Path builders use the returned value, not the raw caller input, so the
    filesystem boundary is both runtime-confined and visible to CodeQL's
    path-injection dataflow.
    """
    validate_strategy_instance_id(value)
    match = _INSTANCE_ID_RE.fullmatch(value)
    if match is None:
        raise ValueError(
            f"strategy_instance_id rejected on second check: {value!r}"
        )
    safe = match.group(0)
    if Path(safe).name != safe:
        raise ValueError(f"strategy_instance_id must be a single path segment: {value!r}")
    return safe


def confine_path_to_root(path: Path, root: Path, *, label: str) -> Path:
    """Return ``path`` only after proving its real path stays under ``root``."""
    root_real = os.path.realpath(os.fspath(root))
    candidate = os.path.realpath(os.fspath(path))
    root_prefix = root_real.rstrip(os.sep) + os.sep
    if candidate != root_real and not candidate.startswith(root_prefix):
        raise ValueError(f"{label} path {candidate} escapes root {root_real}")
    return Path(candidate)


def strategy_instance_artifact_dir(
    artifacts_root: Path, namespace: str, strategy_instance_id: str
) -> Path:
    """Return a confined per-instance directory below ``artifacts_root``.

    ``namespace`` is a trusted literal such as ``live_state`` or
    ``live_instances``. ``strategy_instance_id`` is caller/operator input and
    is reconstructed through :func:`safe_strategy_instance_path_segment` before
    joining. The realpath/root-prefix check catches symlink escapes and
    follows the sanitizer shape CodeQL documents for py/path-injection.
    """
    if not namespace or namespace != Path(namespace).name:
        raise ValueError(f"artifact namespace must be a single path segment: {namespace!r}")
    safe_sid = safe_strategy_instance_path_segment(strategy_instance_id)
    namespace_root = os.path.realpath(
        os.path.join(os.fspath(artifacts_root), namespace)
    )
    candidate = os.path.realpath(os.path.join(namespace_root, safe_sid))
    return confine_path_to_root(
        Path(candidate),
        Path(namespace_root),
        label="strategy instance artifact",
    )
