"""Revocation and the per-stock default: serialized writers and a default read that never offers a cleared pointer (#2696).

Live Postgres only (``POSTGRES_URL_IS_EPHEMERAL=1``); each test owns a stock.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest

from app.research.golden_search.qualifications import (
    QualificationAlreadyRevokedError,
    get_default,
    read_default_qualifications,
    revoke_qualification,
    set_default_cas,
)
from tests._helpers.golden_qualification import PROGRAM, canonical_point, seed_qualification


@pytest.fixture
def symbol(unique: str) -> str:
    return f"GD{unique.upper()}"


async def test_a_revocation_racing_the_first_default_is_serialized(
    conn: asyncpg.Connection, second_conn: asyncpg.Connection, unique: str, symbol: str
) -> None:
    row = await seed_qualification(
        conn,
        qualification_id=f"gq-race-{unique}",
        symbol=symbol,
        params=canonical_point(symbol),
        artifact_digest="1" * 64,
    )
    async with conn.transaction():
        await revoke_qualification(
            conn, qualification_id=row.id, reason="Withdrawn.", actor="local:owner", command_id=f"r-{unique}", now_ms=5
        )
        # No pointer row exists yet for this stock, so only the default's own lock can make the first default wait.
        first_default = asyncio.create_task(
            set_default_cas(
                second_conn,
                program_key=PROGRAM,
                symbol=symbol,
                qualification_id=row.id,
                expected_qualification_id=None,
                reason="approve",
                actor="local:owner",
                now_ms=6,
            )
        )
        await asyncio.sleep(0.3)
        assert not first_default.done()

    with pytest.raises(ValueError, match="revoked"):
        await first_default
    assert await get_default(conn, PROGRAM, symbol) is None


async def test_a_revocation_racing_its_own_retry_answers_both_with_one_event(
    conn: asyncpg.Connection, second_conn: asyncpg.Connection, unique: str, symbol: str
) -> None:
    row = await seed_qualification(
        conn,
        qualification_id=f"gq-retry-{unique}",
        symbol=symbol,
        params=canonical_point(symbol),
        artifact_digest="1" * 64,
        make_default=True,
    )
    command = f"r-{unique}"
    async with conn.transaction():
        first = await revoke_qualification(
            conn, qualification_id=row.id, reason="Withdrawn.", actor="local:owner", command_id=command, now_ms=5
        )
        # The same command, sent again before the first answer arrived (a double submit).
        retry = asyncio.create_task(
            revoke_qualification(
                second_conn,
                qualification_id=row.id,
                reason="Withdrawn.",
                actor="local:owner",
                command_id=command,
                now_ms=6,
            )
        )
        await asyncio.sleep(0.3)
        assert not retry.done()

    replayed = await retry
    assert replayed.event == first.event
    assert first.cleared_default is True
    events = await conn.fetchval(
        "SELECT count(*) FROM research_golden_qualification_events WHERE qualification_id = $1", row.id
    )
    assert events == 1


async def test_a_second_revocation_by_another_command_is_refused(
    conn: asyncpg.Connection, unique: str, symbol: str
) -> None:
    row = await seed_qualification(
        conn,
        qualification_id=f"gq-twice-{unique}",
        symbol=symbol,
        params=canonical_point(symbol),
        artifact_digest="1" * 64,
    )
    await revoke_qualification(
        conn, qualification_id=row.id, reason="Withdrawn.", actor="local:owner", command_id=f"a-{unique}", now_ms=5
    )

    with pytest.raises(QualificationAlreadyRevokedError):
        await revoke_qualification(
            conn, qualification_id=row.id, reason="Again.", actor="local:owner", command_id=f"b-{unique}", now_ms=6
        )


async def test_the_default_read_never_offers_a_cleared_pointer(
    conn: asyncpg.Connection, unique: str, symbol: str
) -> None:
    row = await seed_qualification(
        conn,
        qualification_id=f"gq-read-{unique}",
        symbol=symbol,
        params=canonical_point(symbol),
        artifact_digest="1" * 64,
        make_default=True,
    )
    (before,) = await read_default_qualifications(conn, symbol=symbol)
    assert (before.default.qualification_id, before.qualification.id, before.events) == (row.id, row.id, ())

    revocation = await revoke_qualification(
        conn, qualification_id=row.id, reason="Withdrawn.", actor="local:owner", command_id=f"r-{unique}", now_ms=5
    )

    assert revocation.cleared_default is True
    assert await read_default_qualifications(conn, symbol=symbol) == []
