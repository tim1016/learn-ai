"""Default artifacts root for the research-phase artifact store.

Lives here rather than under ``app/research/runs/storage.py`` so the
non-``runs`` phases (and the shared ``ArtifactStore`` itself) can
import it without leaning on the runs module. PR 4 will rewrite
``runs/storage.py`` entirely; pulling this small utility out first
keeps the cross-module move out of PR 4's single-phase touch scope.

``app.research.runs.storage`` re-exports ``default_artifacts_root``
from this module so the pre-seam callers
(``walk_forward/storage.py``, ``baselines/storage.py``) keep working
unchanged until their own migration PRs.
"""

from __future__ import annotations

import os
from pathlib import Path

ARTIFACTS_ROOT_ENV = "LEARN_AI_ARTIFACTS_ROOT"
"""Env var that overrides the default artifacts root."""


def default_artifacts_root() -> Path:
    """Return the default ``artifacts/runs/`` directory.

    Resolution order:
      1. ``$LEARN_AI_ARTIFACTS_ROOT`` if set (caller is responsible for
         existence; the store creates it on first write).
      2. ``<package_root>/artifacts/runs`` — anchored relative to this
         file so the path is correct regardless of CWD.

    The "package root" is the parent of ``app/`` — i.e.
    ``Path(__file__).resolve().parents[3]``. From
    ``app/research/artifact/root.py`` that climbs:
    ``artifact → research → app → <package_root>``.
    """
    explicit = os.environ.get(ARTIFACTS_ROOT_ENV)
    if explicit:
        return Path(explicit)
    return Path(__file__).resolve().parents[3] / "artifacts" / "runs"
