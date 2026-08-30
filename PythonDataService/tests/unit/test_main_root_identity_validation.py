"""``_validate_data_root_identity`` — the lifespan hook that wires issue
#1876's fail-closed root-identity check into actual app startup.

Before this fix, ``resolve_root_context`` was called only from
``test_root_identity.py``; nothing in ``app.main``'s ``lifespan`` ever
validated the configured root, so a wiped or freshly-mounted root would be
silently adopted rather than aborting startup. ``lifespan`` itself pulls in
broker/IBKR side effects that make it awkward to exercise directly in a
unit test, so the validation step is its own small function and is tested
here in isolation.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from app.config import LEGACY_ROOT_ID, settings
from app.data_lake.root_identity import LakeRootIdentityError, init_empty_root
from app.main import _validate_data_root_identity

_ROOT_A = UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def write_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "writer"
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(root))
    return root


def test_raises_when_no_marker_exists_even_for_the_default_legacy_root_id(write_root: Path) -> None:
    """DATA_LAKE_ROOT_ID unset -> active_root_id() falls back to
    LEGACY_ROOT_ID. That default/back-compat case must still fail closed,
    not be treated as "no configuration, nothing to validate"."""
    with pytest.raises(LakeRootIdentityError):
        _validate_data_root_identity()


def test_raises_on_a_root_id_mismatch(write_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    init_empty_root(write_root, _ROOT_A)
    monkeypatch.setattr(settings, "DATA_LAKE_ROOT_ID", str(LEGACY_ROOT_ID))

    with pytest.raises(LakeRootIdentityError, match="mismatch"):
        _validate_data_root_identity()


def test_passes_when_the_marker_matches_the_configured_root_id(
    write_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    init_empty_root(write_root, _ROOT_A)
    monkeypatch.setattr(settings, "DATA_LAKE_ROOT_ID", str(_ROOT_A))

    _validate_data_root_identity()  # must not raise


def test_passes_for_the_default_legacy_root_id_when_marked_accordingly(write_root: Path) -> None:
    init_empty_root(write_root, LEGACY_ROOT_ID)

    _validate_data_root_identity()  # must not raise
