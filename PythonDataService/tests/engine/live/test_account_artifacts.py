"""Tests for the account-scoped artifact path guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.live.account_artifacts import (
    AccountArtifactError,
    account_artifacts_root,
)


@pytest.mark.parametrize(
    "account_id",
    [
        "",
        "/tmp/DU123456",
        "../x",
        "du123456",
        " DU123456 ",
        "DU.123456",
        "DU-123456",
        "DU 123456",
        "DU/123456",
        "DU%2F123456",
    ],
)
def test_account_artifacts_root_rejects_path_like_account_id(
    tmp_path: Path,
    account_id: str,
) -> None:
    with pytest.raises(AccountArtifactError, match="invalid account_id"):
        account_artifacts_root(tmp_path, account_id)


@pytest.mark.parametrize("account_id", ["9LIVE0001", "DU123456", "123456789"])
def test_account_artifacts_root_accepts_uppercase_alphanumeric_account_ids(
    tmp_path: Path, account_id: str
) -> None:
    """Digit-led ids are real Alpaca live account ids (ADR 0059 slice 7); the segment stays safe."""
    root = account_artifacts_root(tmp_path, account_id)
    assert root == (tmp_path / "accounts" / account_id).resolve()


def test_account_artifacts_root_rejects_symlink_escape(tmp_path: Path) -> None:
    accounts_root = tmp_path / "accounts"
    accounts_root.mkdir()
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    try:
        (accounts_root / "DU123456").symlink_to(outside_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this test environment: {exc}")

    with pytest.raises(AccountArtifactError, match="path traversal"):
        account_artifacts_root(tmp_path, "DU123456")


def test_account_artifacts_root_rejects_sibling_prefix_symlink_escape(tmp_path: Path) -> None:
    accounts_root = tmp_path / "accounts"
    accounts_root.mkdir()
    sibling_root = tmp_path / "accounts-evil" / "DU123456"
    sibling_root.mkdir(parents=True)
    try:
        (accounts_root / "DU123456").symlink_to(sibling_root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable in this test environment: {exc}")

    with pytest.raises(AccountArtifactError, match="path traversal"):
        account_artifacts_root(tmp_path, "DU123456")
