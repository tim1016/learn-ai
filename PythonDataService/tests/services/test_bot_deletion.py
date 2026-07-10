"""Filesystem-boundary regressions for durable bot deletion markers."""

from pathlib import Path

import pytest

from app.services.bot_deletion import stable_bot_deletion_path


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
