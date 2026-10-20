"""Open-Q1 review-fix: shared-secret token helper tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.lean_sidecar import config as sidecar_config
from app.lean_sidecar.launcher_auth import (
    LAUNCHER_TOKEN_FILENAME,
    ensure_launcher_token,
    read_launcher_token,
    token_file_path,
)


@pytest.fixture
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the env override so the file-fallback path is what's tested.

    Operators who set ``LEAN_LAUNCHER_TOKEN`` exercise a different
    branch (verified separately). Most tests here cover the
    auto-generated + on-disk-persisted path that activates when env
    is unset on the host.
    """
    monkeypatch.delenv("LEAN_LAUNCHER_TOKEN", raising=False)


class TestEnsureLauncherToken:
    def test_ensure_launcher_token_when_env_and_file_both_absent(
        self,
        tmp_path: Path,
        _clear_env: None,
    ) -> None:
        first = ensure_launcher_token(tmp_path)
        assert len(first) >= 30, "URL-safe 32-byte token should produce ≥30 chars"
        # File must persist for the data plane to read.
        assert (tmp_path / LAUNCHER_TOKEN_FILENAME).exists(), "token is persisted to file"
        assert (tmp_path / LAUNCHER_TOKEN_FILENAME).read_text(
            encoding="utf-8"
        ).strip() == first, "persisted file holds the generated token"
        leftover = list(tmp_path.glob(".launcher-token-*"))
        assert leftover == [], (
            f"atomic write left tempfile turds: {leftover}"
        )
        # Idempotent — repeated calls don't rotate the token. A
        # launcher restart should pick up the persisted token rather
        # than invalidating in-flight data-plane clients.
        second = ensure_launcher_token(tmp_path)
        assert first == second, "second call returns the persisted token, not a new one"

    def test_env_override_wins_over_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Operator-set ``LEAN_LAUNCHER_TOKEN`` is the source of truth;
        the file-backed token is only the fallback."""
        # Pre-populate the file with a different value so we can
        # tell which one ensure_launcher_token returned.
        (tmp_path / LAUNCHER_TOKEN_FILENAME).write_text(
            "from-file", encoding="utf-8"
        )
        monkeypatch.setenv("LEAN_LAUNCHER_TOKEN", "from-env")
        assert ensure_launcher_token(tmp_path) == "from-env"
        # And the file is not rewritten when env wins.
        assert (tmp_path / LAUNCHER_TOKEN_FILENAME).read_text(
            encoding="utf-8"
        ) == "from-file"

    def test_token_file_has_restricted_mode_on_posix(
        self,
        tmp_path: Path,
        _clear_env: None,
    ) -> None:
        """0600 — only the launcher's owner reads. Skipped on
        Windows where NTFS ACLs don't map cleanly to POSIX modes."""
        if os.name != "posix":
            pytest.skip("POSIX modes are a no-op on Windows + NTFS")
        ensure_launcher_token(tmp_path)
        mode = (tmp_path / LAUNCHER_TOKEN_FILENAME).stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"


class TestReadLauncherToken:
    def test_returns_none_when_neither_env_nor_file_set(
        self,
        tmp_path: Path,
        _clear_env: None,
    ) -> None:
        assert read_launcher_token(tmp_path) is None

    @pytest.mark.parametrize(
        ("file_text", "expected"),
        [
            pytest.param("the-secret\n", "the-secret", id="reads_file_when_env_absent"),
            # An empty/whitespace token file is not a usable token. The
            # data plane treats it as "no token", sends no header, and gets
            # a 401 from the launcher — operator visibility into a corrupt
            # token file rather than silently passing an empty secret.
            pytest.param("   \n", None, id="empty_file_treated_as_no_token"),
        ],
    )
    def test_read_launcher_token_from_file_when_env_absent(
        self,
        tmp_path: Path,
        _clear_env: None,
        file_text: str,
        expected: str | None,
    ) -> None:
        (tmp_path / LAUNCHER_TOKEN_FILENAME).write_text(file_text, encoding="utf-8")
        assert read_launcher_token(tmp_path) == expected

    def test_env_wins_over_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / LAUNCHER_TOKEN_FILENAME).write_text(
            "file-value", encoding="utf-8"
        )
        monkeypatch.setenv("LEAN_LAUNCHER_TOKEN", "env-value")
        assert read_launcher_token(tmp_path) == "env-value"


def test_default_token_file_path_tracks_configured_artifacts_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The data plane must read the token generated for its active root."""
    monkeypatch.setattr(sidecar_config, "DEFAULT_ARTIFACTS_ROOT", tmp_path)

    assert token_file_path() == tmp_path / LAUNCHER_TOKEN_FILENAME
