"""A projection over an absent authority must refuse in the Clerk's own vocabulary.

Regression for the 2026-08-28 clean-slate incident: with the account's
``clerk.db`` gone but the broker account still resolvable, every panel and
catalog route -- including ``/clerk/status``, whose whole job is to report
authority state -- answered ``500`` carrying a raw
``sqlite3.OperationalError: unable to open database file``. A never-existed
account correctly answered ``404`` the whole time, so the 500 was specific to
"real account, no local authority", which is exactly the state a reset leaves
behind and therefore exactly the state an operator is most likely to be
looking at.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.repository import DatabaseMissingAfterEstablishment


def test_reader_over_a_missing_database_raises_the_typed_clerk_error(tmp_path: Path) -> None:
    missing = tmp_path / "accounts" / "alpaca" / "PA-TEST" / "clerk.db"

    with pytest.raises(DatabaseMissingAfterEstablishment) as excinfo:
        SqliteClerkProjectionReader(
            db_path=missing,
            account_id="PA-TEST",
            authority_generation=1,
            db_identity_token="0" * 32,
        )

    assert str(missing) in str(excinfo.value)


async def test_app_translates_clerk_errors_to_a_typed_503_not_a_500() -> None:
    """The boundary must not let a Clerk state escape as an internal error."""
    import json

    # The handler is registered for the Clerk's error base class, so every
    # subclass (missing DB, identity mismatch, schema drift, lease held)
    # inherits the typed answer rather than only the case that prompted it.
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteError, DatabaseMissingAfterEstablishment
    from app.main import app
    from app.utils.error_handlers import clerk_sqlite_exception_handler

    assert app.exception_handlers.get(ClerkSqliteError) is clerk_sqlite_exception_handler

    response = await clerk_sqlite_exception_handler(
        None,  # type: ignore[arg-type]
        DatabaseMissingAfterEstablishment("no authority for PA-TEST"),
    )

    assert response.status_code == 503
    body = json.loads(bytes(response.body))
    assert body["success"] is False
    assert body["detail"]["reason_code"] == "clerk_authority_unusable"
    assert body["detail"]["error_type"] == "DatabaseMissingAfterEstablishment"
    # The body carries a stable operator message, not the exception text.
    # Members of this family interpolate the raw driver error and the db path
    # (``IntegrityCheckFailed``), so echoing ``str(exc)`` re-published exactly
    # what this handler exists to suppress (#1865 review). The detail is
    # logged instead; ``error_type`` still distinguishes the cases.
    assert "no authority for PA-TEST" not in body["detail"]["message"]
    assert "not currently usable" in body["detail"]["message"]


def test_a_database_removed_during_connect_is_still_a_clerk_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file can vanish between the is_file check and the connect.

    A reset running concurrently with a read leaves sqlite raising
    OperationalError from a path that passed its existence check moments
    earlier. Without reclassification the catch-all answers 500 on the very
    endpoint that exists to report an absent authority (#1865 review).
    """
    db_path = tmp_path / "clerk.sqlite3"
    db_path.write_bytes(b"")

    def _vanish(*args: object, **kwargs: object) -> sqlite3.Connection:
        db_path.unlink()
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(sqlite3, "connect", _vanish)

    with pytest.raises(DatabaseMissingAfterEstablishment, match="was removed while opening"):
        SqliteClerkProjectionReader(
            db_path=db_path,
            account_id="PA-TEST",
            authority_generation=1,
            db_identity_token="token",
        )


def test_an_unrelated_sqlite_failure_still_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the missing-database case is reclassified; a real fault is a fault."""
    db_path = tmp_path / "clerk.sqlite3"
    db_path.write_bytes(b"")

    def _boom(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(sqlite3, "connect", _boom)

    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        SqliteClerkProjectionReader(
            db_path=db_path,
            account_id="PA-TEST",
            authority_generation=1,
            db_identity_token="token",
        )
