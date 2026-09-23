"""Identity facts are read raw, from the layouts the canonical code writes (#2268)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.repository import DB_FILENAME
from app.broker.alpaca.clerk.sqlite.writes import confined_account_file
from app.installation_migration.errors import MigrationRefused
from app.installation_migration.facts import (
    CLERK_ACCOUNTS_RELATIVE,
    CLERK_DB_FILENAME,
    read_clerk_volume_facts,
    read_registry_facts,
)


def test_the_clerk_database_layout_matches_the_canonical_writer(tmp_path: Path) -> None:
    """Parity with ``app/broker/alpaca/clerk/sqlite/writes.py::confined_account_file``."""
    (tmp_path / "accounts" / "alpaca" / "PA-1").mkdir(parents=True)

    canonical = confined_account_file(tmp_path, "PA-1", DB_FILENAME)

    assert CLERK_DB_FILENAME == DB_FILENAME
    assert canonical == (tmp_path / CLERK_ACCOUNTS_RELATIVE / "PA-1" / CLERK_DB_FILENAME).resolve()


def test_a_control_volume_without_a_registry_refuses(tmp_path: Path) -> None:
    with pytest.raises(MigrationRefused) as refused:
        read_registry_facts(tmp_path)

    assert refused.value.reason == "registry_missing"


def test_a_lane_volume_without_a_marker_refuses_by_name(tmp_path: Path) -> None:
    with pytest.raises(MigrationRefused) as refused:
        read_clerk_volume_facts("learn-ai-alpaca-clerk-data", tmp_path)

    assert refused.value.reason == "clerk_volume_unmarked"
    assert refused.value.details["volume"] == "learn-ai-alpaca-clerk-data"
