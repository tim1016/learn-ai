"""Phase 3 / VCR-0006 — session_metadata.json sidecar tests."""

from __future__ import annotations

from pathlib import Path


def test_round_trip(tmp_path: Path) -> None:
    from app.engine.live.session_metadata import (
        SESSION_METADATA_SCHEMA_VERSION,
        SessionMetadata,
        read_session_metadata,
        write_session_metadata,
    )

    metadata = SessionMetadata(
        schema_version=SESSION_METADATA_SCHEMA_VERSION,
        ledger_account_id="DU1234567",
        connected_account="DU1234567",
        session_started_ms=1_780_000_000_000,
        session_ended_ms=None,
        connection_epoch=1,
    )
    write_session_metadata(tmp_path, metadata)
    loaded = read_session_metadata(tmp_path)
    assert loaded == metadata


def test_overwrite_replaces_prior_session(tmp_path: Path) -> None:
    """``session_metadata.json`` always reflects the latest session for the
    run_dir — a redeploy under a different IBKR client_id must not leave
    stale forensic data."""
    from app.engine.live.session_metadata import (
        SESSION_METADATA_SCHEMA_VERSION,
        SessionMetadata,
        read_session_metadata,
        write_session_metadata,
    )

    first = SessionMetadata(
        schema_version=SESSION_METADATA_SCHEMA_VERSION,
        ledger_account_id="DU111",
        connected_account="DU111",
        session_started_ms=1_780_000_000_000,
        session_ended_ms=1_780_000_060_000,
        connection_epoch=1,
    )
    write_session_metadata(tmp_path, first)
    second = SessionMetadata(
        schema_version=SESSION_METADATA_SCHEMA_VERSION,
        ledger_account_id="DU111",
        connected_account="DU111",
        session_started_ms=1_780_000_120_000,
        session_ended_ms=None,
        connection_epoch=1,
    )
    write_session_metadata(tmp_path, second)
    assert read_session_metadata(tmp_path) == second


def test_missing_file_returns_none(tmp_path: Path) -> None:
    from app.engine.live.session_metadata import read_session_metadata

    assert read_session_metadata(tmp_path) is None


def test_corrupt_file_returns_none(tmp_path: Path) -> None:
    """An unreadable / malformed sidecar must not crash the read path —
    the caller (LiveEngine) writes a fresh one."""
    from app.engine.live.session_metadata import (
        SESSION_METADATA_FILENAME,
        read_session_metadata,
    )

    (tmp_path / SESSION_METADATA_FILENAME).write_text("{ not json", encoding="utf-8")
    assert read_session_metadata(tmp_path) is None


def test_write_persists_both_raw_account_values(tmp_path: Path) -> None:
    """The forensic record carries BOTH the ledger-side and broker-side
    raw account values so a future audit can verify the identity pair the
    engine saw at start, even if the cockpit displays a different one."""
    import json

    from app.engine.live.session_metadata import (
        SESSION_METADATA_FILENAME,
        SESSION_METADATA_SCHEMA_VERSION,
        SessionMetadata,
        write_session_metadata,
    )

    write_session_metadata(
        tmp_path,
        SessionMetadata(
            schema_version=SESSION_METADATA_SCHEMA_VERSION,
            ledger_account_id="DU1234567",
            connected_account="DU1234567",
            session_started_ms=1_780_000_000_000,
            session_ended_ms=None,
            connection_epoch=2,
        ),
    )
    payload = json.loads((tmp_path / SESSION_METADATA_FILENAME).read_text(encoding="utf-8"))
    assert payload["ledger_account_id"] == "DU1234567"
    assert payload["connected_account"] == "DU1234567"
    assert payload["connection_epoch"] == 2
