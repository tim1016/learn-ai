"""Cases the option lake tests share (#3030): when a session may be captured, what a database refuses, a publish race, a slow listing.

Each is here once because more than one test module needs it, and a copy in each
would be a second place to get a hand-worked figure or a timing wrong.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg
import httpx
import pytest

from app.data_lake import catalog_client
from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
from app.data_lake.types import ArtifactIdentity, ArtifactRecord

# A session's option data may be captured from New York midnight after its date. Each row is a
# session, its label, and that midnight as epoch ms UTC, worked by hand from the New York offset in
# force at that midnight (UTC-5 in winter, UTC-4 in summer):
#
# * Friday 2025-03-07 -> Saturday 03-08 00:00 EST = 05:00 UTC. The Monday after the clocks went
#   forward (03-09) -> Tuesday 03-11 00:00 EDT = 04:00 UTC. A fixed -05:00 gets the Monday an hour late.
# * Friday 2025-10-31 -> Saturday 11-01 00:00 EDT = 04:00 UTC. The Monday after the clocks went back
#   (11-02) -> Tuesday 11-04 00:00 EST = 05:00 UTC. A fixed -04:00 gets the Monday an hour early.
# * Friday 2025-06-13 -> Saturday 06-14 00:00 EDT = 04:00 UTC.
# * Half days close at 13:00 and follow the same rule: Friday 2025-11-28 (closes 18:00 UTC) -> Saturday
#   11-29 00:00 EST = 05:00 UTC; Thursday 2025-07-03 (closes 17:00 UTC) -> Friday 07-04 00:00 EDT = 04:00 UTC.
CAPTURE_OPENS: tuple[tuple[str, date, int], ...] = (
    ("friday before the clocks go forward", date(2025, 3, 7), 1741410000000),
    ("monday after the clocks go forward", date(2025, 3, 10), 1741665600000),
    ("friday before the clocks go back", date(2025, 10, 31), 1761969600000),
    ("monday after the clocks go back", date(2025, 11, 3), 1762232400000),
    ("an ordinary summer friday", date(2025, 6, 13), 1749873600000),
    ("half day after thanksgiving", date(2025, 11, 28), 1764392400000),
    ("half day before the fourth of july", date(2025, 7, 3), 1751601600000),
)

#: The session close of the two half days, 13:00 New York (hand-worked as above), for the tests that
#: show nothing is captured between a session's close and the midnight that ends its date.
HALF_DAY_CLOSE_MS = {date(2025, 11, 28): 1764352800000, date(2025, 7, 3): 1751562000000}


def one_bar_file(price: str, symbol: str = "SPY250613C00600000") -> bytes:
    """The file of a one-bar 2025-06-13 session at ``price``: two writers of one day with different bytes tell their files apart."""
    bar = MinuteTradeBar(
        bar_start_et=datetime(2025, 6, 13, 9, 30, tzinfo=ZoneInfo("America/New_York")), open=Decimal(price),
        high=Decimal(price), low=Decimal(price), close=Decimal(price), volume=Decimal(1),
    )
    return build_minute_trade_zip_bytes(symbol, "20250613", [bar])


def pieces_of_the_key_in(key: str, *texts: str) -> set[str]:
    """Every four characters in a row of ``key`` that any of ``texts`` holds.

    A malformed key's message, log line or stored error must name none of the key, not even a part;
    the key used for it is made of characters no message of ours spells (a sentinel token).
    """
    shown = "".join(texts)
    return {key[i : i + 4] for i in range(len(key) - 3) if key[i : i + 4] in shown}


def refuse_what_postgres_refuses(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make ``catalog_client.fail_artifact`` raise as the real table does for text it cannot hold.

    Postgres refuses a NUL in text (asyncpg: ``CharacterNotInRepertoireError``), and asyncpg cannot
    encode a lone surrogate (``UnicodeEncodeError``). Install the catalog (fake or real) first; the
    returned list receives each text that was accepted, so a test can see what was stored.
    """
    stored: list[str] = []
    accepted = catalog_client.fail_artifact

    async def fail_artifact(artifact_id: int, last_error: str, error_message: str | None = None, **kwargs: Any) -> bool:
        if error_message is not None:
            if "\x00" in error_message:
                raise asyncpg.exceptions.CharacterNotInRepertoireError("invalid byte sequence for encoding \"UTF8\": 0x00")
            error_message.encode("utf-8")
            stored.append(error_message)
        return await accepted(artifact_id, last_error, error_message, **kwargs)

    monkeypatch.setattr(catalog_client, "fail_artifact", fail_artifact)
    return stored


def publish_stale_writer_first_once_both_have_staged(monkeypatch: pytest.MonkeyPatch, *, winner_generation: int) -> None:
    """Order two concurrent publishes of one row: both stage, then the stale writer's turn at the catalog, then the winner's.

    Staging is outside the catalog, so two writers of one file stage together; what happens at the
    catalog (the stale writer is refused and cleans up, the winner promotes) is then the order the
    scheduler happened to choose. This fixes it to the order that hurts: the stale writer finishes
    its refusal and its cleanup before the winner promotes. Everything else is the real code.
    """
    publish: Callable[..., Awaitable[None]] = catalog_client.publish_under_lease
    staged = 0
    both_staged = asyncio.Event()
    stale_done = asyncio.Event()

    async def ordered(**kwargs: Any) -> None:
        nonlocal staged
        staged += 1
        if staged == 2:
            both_staged.set()
        await both_staged.wait()
        is_winner = kwargs["lease_generation"] == winner_generation
        if is_winner:
            await stale_done.wait()
        try:
            await publish(**kwargs)
        finally:
            if not is_winner:
                stale_done.set()

    monkeypatch.setattr(catalog_client, "publish_under_lease", ordered)


class MovableClock:
    """A clock a test moves by hand, in seconds: what the listing fetch's budget runs on when the test replaces ``_now_s``."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def slow_listing_pages(
    clock: MovableClock, *, pages: int, seconds_per_page: float, contracts_url: str
) -> Callable[[httpx.Request], httpx.Response]:
    """A respx side effect: the listing is ``pages`` pages of one contract each, and every answer takes ``seconds_per_page`` on ``clock``.

    The first request has no cursor and the cursor ``p<k>`` asks for page ``k + 1``, so the number of
    requests made is the number of pages asked. Contract ``k`` is the SPY call struck at ``375 + k``.
    """

    def answer(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        page = 1 if cursor is None else int(cursor.removeprefix("p")) + 1
        clock.now += seconds_per_page
        strike = 375 + page
        row = {
            "ticker": f"O:SPY250613C{strike * 1000:08d}", "contract_type": "call", "strike_price": strike,
            "expiration_date": "2025-06-13", "exercise_style": "american", "shares_per_contract": 100,
        }
        body: dict[str, Any] = {"status": "OK", "request_id": "r", "results": [row]}
        if page < pages:
            body["next_url"] = f"{contracts_url}?cursor=p{page}"
        return httpx.Response(200, json=body)

    return answer


async def complete_row_of_another_provider(
    record: ArtifactRecord, *, provider: str = "alpaca", data_contract_hash: str | None = None, file_path: str | None = None
) -> int:
    """A complete minute-bar row of ``provider`` for the same contract-day as ``record``, on whichever catalog is installed.

    A provider is part of a minute-bar row's identity, so two providers' rows for one contract-day sit side by
    side. By default the row describes the very same file under the very same recipe, which is the row a reuse
    would most like to take; ``data_contract_hash`` and ``file_path`` make it a different one.
    """
    identity = ArtifactIdentity(
        artifact_kind="time_series_bars", market=record.market, symbol=record.symbol, trading_date=record.trading_date,
        resolution="minute", data_type="trade", provider=provider, price_adjustment_mode=record.price_adjustment_mode,
    )
    recipe = data_contract_hash or record.data_contract_hash
    artifact_id = await catalog_client.claim_minute_bar(
        identity=identity, worker_id="another-worker", lease_ttl_ms=300_000, data_contract_hash=recipe,
        file_path=file_path or record.file_path,
    )
    assert artifact_id is not None
    assert await catalog_client.complete_artifact(
        artifact_id, record.row_count, record.first_bar_start_ms, record.last_bar_start_ms, record.file_size_bytes,
        record.file_sha256, lease_generation=catalog_client.INITIAL_LEASE_GENERATION, data_contract_hash=recipe,
    )
    return artifact_id
