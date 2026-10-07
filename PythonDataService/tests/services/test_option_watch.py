"""What option bots ask to have watched reaches the IBKR supervisor their Clerk process builds."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.broker.alpaca.config import AlpacaSettings
from app.broker.alpaca.market_liveness import (
    AlpacaMarketLivenessConsumer,
    get_option_quotes,
    set_market_liveness_consumer,
)
from app.broker.capture.journal import CaptureJournal
from app.broker.ibkr.config import reset_settings_for_testing
from app.broker.ibkr.option_quotes import WatchRequest
from app.options.watch import LineClass
from app.services.market_liveness import MarketLivenessStore
from app.services.option_watch import OptionWatchList

# The resolver's own scenes, for a plan and a contract.
from tests.options import test_resolver as scenes


@pytest.fixture
def ibkr_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """IBKR settings read fresh from this test's environment, and put back after."""
    reset_settings_for_testing()
    yield monkeypatch
    reset_settings_for_testing()


def _consumer(tmp_path: Path, watch: OptionWatchList) -> AlpacaMarketLivenessConsumer:
    return AlpacaMarketLivenessConsumer.for_alpaca(
        read=object(),  # type: ignore[arg-type]
        settings=AlpacaSettings(api_key_id="paper-key", api_secret_key="paper-secret", mode="paper"),
        store=MarketLivenessStore(),
        journal=CaptureJournal(capture_dir=tmp_path / "capture"),
        option_watch=watch,
    )


def test_a_later_request_from_the_same_asker_replaces_its_earlier_one_and_leaving_takes_it_away() -> None:
    watch = OptionWatchList()
    first = WatchRequest("SPY", LineClass.CANDIDATE, scenes._one("call", {"selector": "atm"}), Decimal("585.00"))
    moved = WatchRequest("SPY", LineClass.CANDIDATE, first.plan, Decimal("586.20"))
    held = WatchRequest("QQQ", LineClass.CANDIDATE, held=(scenes._contract("call", 585),))

    watch.watch("bot-1", first)
    watch.watch("bot-2", held)
    watch.watch("bot-1", moved)
    both = watch.requests()
    watch.unwatch("bot-1")
    watch.unwatch("never-asked")

    assert both == (moved, held)
    assert watch.requests() == (held,)


async def test_the_supervisor_a_clerk_builds_watches_what_its_bots_ask_inside_the_budget_it_is_set(
    tmp_path: Path, ibkr_settings: pytest.MonkeyPatch
) -> None:
    """A bot asks for options on QQQ to be watched. With no Gateway connected nothing can be quoted, but the
    supervisor already counts QQQ's own quote among what it must watch, and says the option feed is not up."""
    ibkr_settings.setenv("IBKR_OPTION_LINE_BUDGET", "40")
    not_connected = Mock()
    not_connected.is_connected.return_value = False
    ibkr_settings.setattr("app.broker.ibkr.client._client", not_connected)
    watch = OptionWatchList()
    consumer = _consumer(tmp_path, watch)
    supervisor = consumer.option_quotes
    try:
        before = await supervisor()
        watch.watch(
            "bot-1", WatchRequest("QQQ", LineClass.CANDIDATE, scenes._one("call", {"selector": "atm"}), Decimal("585.00"))
        )
        after = await supervisor()

        assert [seen.symbol for seen in before.subscriptions] == []
        assert [seen.symbol for seen in after.subscriptions] == ["QQQ"]
        assert supervisor.option_line_use().budget == 40
        assert supervisor.option_feed().unhealthy is not None
        assert supervisor.option_chain("QQQ") is None
    finally:
        await supervisor.close()


async def test_with_no_budget_set_a_clerk_asks_ibkr_for_no_option_line(
    tmp_path: Path, ibkr_settings: pytest.MonkeyPatch
) -> None:
    ibkr_settings.delenv("IBKR_OPTION_LINE_BUDGET", raising=False)
    supervisor = _consumer(tmp_path, OptionWatchList()).option_quotes

    assert supervisor.option_line_use().budget == 0


def test_option_bots_read_their_quotes_from_the_supervisor_of_the_clerk_running_here(
    tmp_path: Path, ibkr_settings: pytest.MonkeyPatch
) -> None:
    consumer = _consumer(tmp_path, OptionWatchList())
    try:
        nothing_running = get_option_quotes()
        set_market_liveness_consumer(consumer)

        assert nothing_running is None
        assert get_option_quotes() is consumer.option_quotes
    finally:
        set_market_liveness_consumer(None)


# ---- A page asks on a lease ---------------------------------------------------


def _page_wants(strike: dict) -> WatchRequest:
    return WatchRequest("SPY", LineClass.PREVIEW, scenes._one("call", strike))


def test_a_request_on_a_lease_is_gone_when_the_lease_runs_out_and_a_bots_request_is_not() -> None:
    now = [1_000_000]
    watch = OptionWatchList(clock=lambda: now[0])
    bot = WatchRequest("SPY", LineClass.CANDIDATE, scenes._one("call", {"selector": "atm"}), Decimal("585.00"))
    page = _page_wants({"selector": "atm"})
    watch.watch("bot-1", bot)
    watch.watch_on_lease("page-1", page, lease_ms=60_000)

    now[0] += 59_999
    just_inside = watch.requests()
    now[0] += 1

    assert just_inside == (bot, page)
    assert watch.requests() == (bot,)
    assert watch.on_lease() == ()


def test_asking_again_renews_a_lease_and_keeps_when_the_same_request_was_first_asked() -> None:
    now = [1_000_000]
    watch = OptionWatchList(clock=lambda: now[0])
    page = _page_wants({"selector": "atm"})

    first = watch.watch_on_lease("page-1", page, lease_ms=60_000)
    now[0] += 40_000
    renewed = watch.watch_on_lease("page-1", _page_wants({"selector": "atm"}), lease_ms=60_000)
    now[0] += 40_000
    still_held = watch.requests()
    changed = watch.watch_on_lease("page-1", _page_wants({"selector": "atm_offset", "offset": 1}), lease_ms=60_000)

    assert (first, renewed) == (1_000_000, 1_000_000)
    assert still_held == (page,)
    # A changed request is a new window: its quotes have had no time to arrive.
    assert changed == 1_080_000


def test_a_lease_that_ran_out_starts_again_from_now() -> None:
    now = [1_000_000]
    watch = OptionWatchList(clock=lambda: now[0])
    page = _page_wants({"selector": "atm"})
    watch.watch_on_lease("page-1", page, lease_ms=60_000)

    now[0] += 60_000

    assert watch.watch_on_lease("page-1", page, lease_ms=60_000) == 1_060_000


def test_leases_are_listed_soonest_to_end_first_and_a_plain_request_under_a_leased_key_ends_its_lease() -> None:
    now = [1_000_000]
    watch = OptionWatchList(clock=lambda: now[0])
    page = _page_wants({"selector": "atm"})
    watch.watch_on_lease("late", page, lease_ms=90_000)
    watch.watch_on_lease("soon", page, lease_ms=30_000)

    listed = watch.on_lease()
    watch.watch("soon", page)
    now[0] += 1_000_000

    assert listed == ("soon", "late")
    assert watch.requests() == (page,)
    with pytest.raises(ValueError):
        watch.watch_on_lease("late", page, lease_ms=0)


# ---- Pages held to a number of leases ------------------------------------------------


def _three_pages_held_to_two() -> tuple[OptionWatchList, list[int], WatchRequest]:
    """Two pages hold the two leases there may be, asked a second apart. A third asks a second later still."""
    now = [1_000_000]
    watch = OptionWatchList(clock=lambda: now[0])
    page = _page_wants({"selector": "atm"})
    for key in ("first", "second"):
        watch.watch_on_lease(key, page, lease_ms=60_000, at_most=2)
        now[0] += 1_000
    return watch, now, page


def test_a_page_past_the_most_leases_takes_the_one_asked_for_longest_ago() -> None:
    watch, now, page = _three_pages_held_to_two()

    since = watch.watch_on_lease("third", page, lease_ms=60_000, at_most=2)

    assert since == now[0]
    assert watch.on_lease() == ("second", "third")
    assert watch.requests() == (page, page)


def test_the_page_that_gave_way_is_told_so_and_takes_no_lease_while_all_are_taken() -> None:
    """All three keep asking, a second apart, for a minute and more. The same two hold their leases throughout."""
    watch, now, page = _three_pages_held_to_two()
    watch.watch_on_lease("third", page, lease_ms=60_000, at_most=2)
    since = {key: watch.watch_on_lease(key, page, lease_ms=60_000, at_most=2) for key in ("second", "third")}

    for _ in range(90):
        now[0] += 1_000
        told = {key: watch.watch_on_lease(key, page, lease_ms=60_000, at_most=2) for key in ("first", "second", "third")}

        assert told == {"first": None, **since}
        assert sorted(watch.on_lease()) == ["second", "third"]


def test_the_page_that_gave_way_is_let_in_when_a_lease_ends() -> None:
    watch, now, page = _three_pages_held_to_two()
    watch.watch_on_lease("third", page, lease_ms=60_000, at_most=2)

    # The second page stops asking. The other two ask on.
    for _ in range(6):
        now[0] += 10_000
        told = watch.watch_on_lease("first", page, lease_ms=60_000, at_most=2)
        watch.watch_on_lease("third", page, lease_ms=60_000, at_most=2)

    assert told is not None
    assert sorted(watch.on_lease()) == ["first", "third"]


def test_a_page_that_gave_way_and_went_quiet_is_a_new_page_when_it_comes_back() -> None:
    """Its giving way is remembered only while it keeps asking. A minute on it asks as any new page does."""
    watch, now, page = _three_pages_held_to_two()
    watch.watch_on_lease("third", page, lease_ms=60_000, at_most=2)

    for _ in range(6):
        now[0] += 10_000
        for key in ("second", "third"):
            watch.watch_on_lease(key, page, lease_ms=60_000, at_most=2)
    back = watch.watch_on_lease("first", page, lease_ms=60_000, at_most=2)

    assert back == now[0]
    assert sorted(watch.on_lease()) == ["first", "third"]


def test_a_page_that_takes_its_request_away_is_not_remembered_as_having_given_way() -> None:
    watch, now, page = _three_pages_held_to_two()
    watch.watch_on_lease("third", page, lease_ms=60_000, at_most=2)

    watch.unwatch("first")
    back = watch.watch_on_lease("first", page, lease_ms=60_000, at_most=2)

    assert back == now[0] and "first" in watch.on_lease()
    with pytest.raises(ValueError):
        watch.watch_on_lease("first", page, lease_ms=60_000, at_most=0)
