"""Tests for the host-daemon shared-secret token (ADR 0007)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.engine.live import daemon_auth
from app.engine.live.daemon_auth import (
    CLERK_HOST_BINDING_CAPABILITY_FILENAME,
    TOKEN_ENV_VAR,
    TOKEN_FILENAME,
    clerk_host_binding_capability_file_path,
    ensure_clerk_host_binding_capability,
    ensure_daemon_token,
    read_daemon_token,
)


@pytest.fixture
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)


class TestEnsureDaemonToken:
    def test_generates_and_persists_token_when_env_and_file_absent(
        self, tmp_path: Path, _clear_env: None
    ) -> None:
        token = ensure_daemon_token(tmp_path)

        assert len(token) >= 30
        persisted = (tmp_path / TOKEN_FILENAME).read_text(encoding="utf-8").strip()
        assert persisted == token

    def test_idempotent_does_not_rotate(self, tmp_path: Path, _clear_env: None) -> None:
        first = ensure_daemon_token(tmp_path)
        second = ensure_daemon_token(tmp_path)
        assert first == second

    def test_env_override_wins_over_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / TOKEN_FILENAME).write_text("from-file", encoding="utf-8")
        monkeypatch.setenv(TOKEN_ENV_VAR, "from-env")
        assert ensure_daemon_token(tmp_path) == "from-env"

    def test_atomic_write_leaves_no_tempfile(self, tmp_path: Path, _clear_env: None) -> None:
        ensure_daemon_token(tmp_path)
        assert list(tmp_path.glob(".host-daemon-token-*")) == []

    def test_token_write_syncs_parent_directory_after_replace(
        self,
        tmp_path: Path,
        _clear_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        synced_paths: list[Path] = []
        monkeypatch.setattr(daemon_auth, "fsync_parent_dir", synced_paths.append)

        ensure_daemon_token(tmp_path)

        assert synced_paths == [tmp_path / TOKEN_FILENAME]

    def test_token_file_mode_is_restricted_on_posix(self, tmp_path: Path, _clear_env: None) -> None:
        if os.name != "posix":
            pytest.skip("POSIX modes are a no-op on Windows")
        ensure_daemon_token(tmp_path)
        mode = (tmp_path / TOKEN_FILENAME).stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"


class TestReadDaemonToken:
    def test_returns_none_when_absent(self, tmp_path: Path, _clear_env: None) -> None:
        assert read_daemon_token(tmp_path) is None

    def test_reads_file_token(self, tmp_path: Path, _clear_env: None) -> None:
        written = ensure_daemon_token(tmp_path)
        assert read_daemon_token(tmp_path) == written

    def test_env_override_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / TOKEN_FILENAME).write_text("from-file", encoding="utf-8")
        monkeypatch.setenv(TOKEN_ENV_VAR, "from-env")
        assert read_daemon_token(tmp_path) == "from-env"

    def test_empty_file_reads_as_none(self, tmp_path: Path, _clear_env: None) -> None:
        (tmp_path / TOKEN_FILENAME).write_text("   \n", encoding="utf-8")
        assert read_daemon_token(tmp_path) is None


def test_clerk_binding_capability_is_durable_and_distinct_from_daemon_http_token(
    tmp_path: Path,
    _clear_env: None,
) -> None:
    http_token = ensure_daemon_token(tmp_path)
    first = ensure_clerk_host_binding_capability(tmp_path)
    second = ensure_clerk_host_binding_capability(tmp_path)

    assert first == second
    assert first != http_token
    assert clerk_host_binding_capability_file_path(tmp_path) == (
        tmp_path / CLERK_HOST_BINDING_CAPABILITY_FILENAME
    )
