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
