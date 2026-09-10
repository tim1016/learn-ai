"""The profiles database is located exactly where ``AlpacaSettings`` says.

``runtime.resolve_clerk_dir`` duplicates ``AlpacaSettings.clerk_dir`` for one
reason: locating a *directory* must not require Alpaca credentials to be
present (ADR 0060 Decision 7). CLAUDE.md guiding philosophy #5 admits a
duplicate only with a parity test naming the canonical file, which is this.

Canonical implementation: ``app/broker/alpaca/config.py`` (``AlpacaSettings.clerk_dir``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.config import AlpacaSettings
from app.broker_configuration.runtime import CLERK_DIR_ENV_VAR, resolve_clerk_dir
from app.broker_configuration.store import DATABASE_DIRECTORY, profiles_database_path

_CREDENTIALS = {"api_key_id": "not-a-real-key", "api_secret_key": "not-a-real-secret"}


def test_default_clerk_dir_matches_alpaca_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CLERK_DIR_ENV_VAR, raising=False)

    assert resolve_clerk_dir() == AlpacaSettings(**_CREDENTIALS).clerk_dir


def test_overridden_clerk_dir_matches_alpaca_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(CLERK_DIR_ENV_VAR, str(tmp_path / "elsewhere"))

    assert resolve_clerk_dir() == AlpacaSettings(**_CREDENTIALS).clerk_dir


def test_resolving_the_clerk_dir_needs_no_alpaca_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reason the duplicate exists, asserted rather than described."""
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    monkeypatch.setenv(CLERK_DIR_ENV_VAR, str(tmp_path / "credential-free"))

    assert resolve_clerk_dir() == tmp_path / "credential-free"


def test_the_database_lives_on_the_clerk_volume_beside_accounts(tmp_path: Path) -> None:
    path = profiles_database_path(tmp_path)

    assert path.parent.parent == tmp_path
    assert path.parent.name == DATABASE_DIRECTORY
    # Not inside any account's custody database, and not under accounts/.
    assert "accounts" not in path.parts
