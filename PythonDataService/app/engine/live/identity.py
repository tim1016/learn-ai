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

import re

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
