"""Filesystem-boundary regressions for durable bot deletion markers."""

from pathlib import Path

import pytest

from app.engine.live.identity import _INSTANCE_ID_RE as canonical_instance_id_re
from app.services.bot_deletion import (
    _BOT_DELETION_INSTANCE_ID_RE,
    stable_bot_deletion_path,
)


def test_bot_deletion_path_regex_matches_canonical_identity_contract() -> None:
    assert _BOT_DELETION_INSTANCE_ID_RE.pattern == canonical_instance_id_re.pattern


def test_stable_bot_deletion_path_confines_symlinked_instance_directory(
    tmp_path: Path,
) -> None:
    artifacts_root = tmp_path / "artifacts"
    live_state_root = artifacts_root / "live_state"
    outside = tmp_path / "outside"
    live_state_root.mkdir(parents=True)
    outside.mkdir()
    (live_state_root / "safe-bot").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes root"):
        stable_bot_deletion_path(artifacts_root, "safe-bot")


def test_stable_bot_deletion_path_uses_validated_single_segment(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="path separator"):
        stable_bot_deletion_path(tmp_path, "../escape")
