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
    assert "no authority for PA-TEST" in body["detail"]["message"]
