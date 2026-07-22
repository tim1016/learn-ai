"""Tests for the shared ``strategy_instance_id`` validator.

The validator is the single creation-time guard that keeps a deployment name in
lockstep with what the operate endpoints (``status`` / ``start`` / ``stop``)
will accept, so a run is never created under a name that can never be operated
on. See ``app.engine.live.identity``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.live.identity import (
    confine_path_to_root,
    strategy_instance_artifact_dir,
    validate_strategy_instance_id,
)


@pytest.mark.parametrize(
    "value",
    [
        "deployment-validation-jun3",
        "spy_ema_crossover",
        "a",
        "Bot.1",
        "X" * 128,
    ],
)
def test_validate_strategy_instance_id_accepts_valid(value: str) -> None:
    assert validate_strategy_instance_id(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "Deploy morning Jun 3",  # internal spaces — the reported bug
        "",  # empty
        " leading",
        "trailing ",
        "..",
        ".",
        "a/b",
        "a\\b",
        "-startshyphen",  # must start with a letter or digit
        "bad@name",
        "X" * 129,  # too long
        "nul\x00byte",
    ],
)
def test_validate_strategy_instance_id_rejects_invalid(value: str) -> None:
    with pytest.raises(ValueError):
        validate_strategy_instance_id(value)


def test_strategy_instance_artifact_dir_confines_below_root(tmp_path: Path) -> None:
    result = strategy_instance_artifact_dir(tmp_path, "live_state", "spy_ema")
    accounts_root = (tmp_path / "live_state").resolve()

    assert result == accounts_root / "spy_ema"
    assert str(result).startswith(f"{accounts_root}/")


def test_strategy_instance_artifact_dir_rejects_symlink_escape(tmp_path: Path) -> None:
    namespace_root = tmp_path / "live_state"
    namespace_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (namespace_root / "spy_ema").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this test environment: {exc}")

    with pytest.raises(ValueError, match="escapes root"):
        strategy_instance_artifact_dir(tmp_path, "live_state", "spy_ema")


def test_confine_path_to_root_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(ValueError, match="escapes root"):
        confine_path_to_root(tmp_path / "elsewhere" / "wal", root, label="test")


def test_confine_path_to_root_allows_confined_file(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    confined = confine_path_to_root(root / "wal.log", root, label="test")

    assert confined == (root / "wal.log").resolve()


def test_instance_id_pattern_matches_operate_endpoint_guard() -> None:
    """Single source of truth: the creation-time pattern in ``identity`` must
    stay byte-identical to the operate-endpoint guard in ``live_instances`` so a
    name accepted at deploy is never rejected at status/start/stop (and vice
    versa). The router keeps its own literal so CodeQL recognises the
    path-injection barrier; this test pins them in lockstep."""
    from app.engine.live.identity import _INSTANCE_ID_RE as creation_re
    from app.routers.live_instances import _INSTANCE_ID_RE as operate_re

    assert creation_re.pattern == operate_re.pattern
