"""The bot page's one-line run summary is a pure function of its facts (#2794 R1)."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.synthesized_orders import SynthesizedAnchor
from app.broker.contract.models import OrderSide
from app.schemas.bot_end import BotEnd, BotEndView, RecordedEnd
from app.schemas.bot_page import HeldPositionFact, RunSummaryFacts
from app.services.bot_end import bot_end_view, bot_end_view_waiting_for_operator
from app.services.broker_v2_panel import panel_data_source
from app.services.broker_v2_panel.bot_page_projection import (
    RunFacts,
    run_facts,
    run_fill_views,
    run_summary,
    run_summary_text,
)
from app.services.broker_v2_panel.feed_continuity_projection import duration_label
from app.services.source_bar_ledger import RECOVERY_QUOTE_PROVIDER, RUN_END_CLOSE_PROVIDER

# Wed Sep 30 2026, 14:30 and 15:59 ET; the line is written that evening, or a year later.
_START = 1_790_793_000_000
_END = 1_790_798_340_000
_WRITTEN = _END + 3_600_000
_A_YEAR_LATER = _END + 366 * 86_400_000


def _facts(**overrides: object) -> RunSummaryFacts:
    values: dict[str, object] = {
        "run_id": "run-1",
        "started_at_ms": _START,
        "ended_at_ms": _END,
        "scheduled_end_at_ms": None,
        "ending": "on_schedule",
        "decision_count": 5,
        "trade_count": 0,
        "set_aside_usd": None,
        "returned_usd": "800.00",
        "held": [],
        "exit_queued": False,
        "authored_at_ms": _WRITTEN,
    }
    return RunSummaryFacts.model_validate(values | overrides)


@pytest.mark.parametrize(
    ("overrides", "text"),
    [
        (
            {},
            "Ran Wed Sep 30, 14:30–15:59 ET · ended on schedule · 5 decisions, no trades · $800.00 back to the account.",
        ),
        (
            {"ending": "stopped", "decision_count": 1, "trade_count": 2, "returned_usd": None,
             "held": [HeldPositionFact(symbol="SPY", quantity="1")], "exit_queued": True},
            "Ran Wed Sep 30, 14:30–15:59 ET · stopped · 1 decision, 2 trades · holds 1 SPY · a sale is queued.",
        ),
        (
            # A closing order the Clerk could not confirm is said as that, never as a sale that is queued (#2854).
            {"ending": "stopped", "decision_count": 1, "trade_count": 2, "returned_usd": None,
             "held": [HeldPositionFact(symbol="SPY", quantity="1")], "exit_queued": True, "exit_unconfirmed": True},
            "Ran Wed Sep 30, 14:30–15:59 ET · stopped · 1 decision, 2 trades · holds 1 SPY · "
            "a closing order is unconfirmed.",
        ),
        (
            {"ending": "running", "ended_at_ms": None, "scheduled_end_at_ms": _END, "returned_usd": None,
             "set_aside_usd": "800.00", "decision_count": 0},
            "Running since Wed Sep 30, 14:30 ET · ends 15:59 ET · no decisions, no trades · $800.00 set aside.",
        ),
        (
            # A stop that recorded no release says nothing about the money.
            {"ending": "crashed", "returned_usd": None},
            "Ran Wed Sep 30, 14:30–15:59 ET · crashed · 5 decisions, no trades.",
        ),
        (
            # A run that ended on a later day names that day; a run from another year names its year.
            {"ended_at_ms": _END + 86_400_000, "authored_at_ms": _A_YEAR_LATER},
            "Ran Wed Sep 30 2026, 14:30 ET – Thu Oct 1 2026, 15:59 ET · ended on schedule · 5 decisions, "
            "no trades · $800.00 back to the account.",
        ),
        (
            # A run whose end recorded no outcome claims no cause.
            {"ending": "ended"},
            "Ran Wed Sep 30, 14:30–15:59 ET · ended · 5 decisions, no trades · $800.00 back to the account.",
        ),
        (
            # A run past the Clerk's decision retention counts at least what it kept.
            {"decision_count": 1000, "decision_count_is_floor": True, "trade_count": 1234},
            "Ran Wed Sep 30, 14:30–15:59 ET · ended on schedule · at least 1,000 decisions, 1,234 trades · "
            "$800.00 back to the account.",
        ),
        ({"ending": "not_started", "started_at_ms": None, "ended_at_ms": None}, "Not started yet."),
        (
            {"ending": "unreadable", "started_at_ms": None, "ended_at_ms": None},
            "This bot's latest run record could not be read.",
        ),
    ],
)
def test_the_summary_line_is_written_from_its_facts(overrides: dict[str, object], text: str) -> None:
    facts = _facts(**overrides)

    assert run_summary_text(facts) == text
    # The same facts give the same line.
    assert run_summary_text(RunSummaryFacts.model_validate(facts.model_dump())) == text


def _stopped_holding_ten(end: BotEndView) -> SimpleNamespace:
    """What the summary reads of the panel: a stopped bot that holds 10 SPY, and its end."""
    return SimpleNamespace(health=SimpleNamespace(running=False), end=end, exposure={"SPY": 10.0})


def test_the_operators_working_sale_is_told_while_the_end_waits_for_the_operator() -> None:
    """#2854: an end that waits for the operator sent no order and queues no sale. The operator's
    own Flatten is a sale the Clerk is working like any other, so while it works the line says a
    sale is queued, exactly as it does for a stopped bot with no end."""
    no_end = bot_end_view(None, now_ms=_WRITTEN, dry_run=False, running=False, use_rth=True)
    reached = bot_end_view(
        RecordedEnd.scheduled(BotEnd(end_at_ms=_END)), now_ms=_WRITTEN, dry_run=False, running=False, use_rth=True,
    )
    waiting = bot_end_view_waiting_for_operator(reached, why="The end's sale of 10 SPY was not sent.")
    flatten_working = RunFacts(
        run_id="run-1", started_at_ms=_START, ended_at_ms=_END, ending_kind="STOPPED", unreadable=False,
        decision_count=5, decision_count_is_floor=False, trade_count=1, orders_sent=1,
        committed_cents=None, released_cents=None, exit_in_progress=True,
    )

    summary = run_summary(_stopped_holding_ten(waiting), flatten_working, now_ms=_WRITTEN)

    assert summary.facts.exit_queued is True
    assert summary.text == (
        "Ran Wed Sep 30, 14:30–15:59 ET · stopped · 5 decisions, 1 trade · holds 10 SPY · a sale is queued."
    )
    assert summary.text == run_summary(_stopped_holding_ten(no_end), flatten_working, now_ms=_WRITTEN).text


def _fill(event_key: str, quantity: float) -> FillRecord:
    return FillRecord(
        account_id="PA1", sid="spy-bot", intent_id="intent-1", order_ref="alpaca/intent-1", event_key=event_key,
        symbol="SPY", side="buy", quantity=quantity, fill_price=100.0, filled_at_ms=_START, fee=None,
    )


def test_each_effective_fill_is_a_trade_as_history_counts_it() -> None:
    """Codex on #2804: an order filled in two parts is two trades, as Home and History count them."""
    facts = run_facts(
        None,
        run_fills=[_fill("execution:e-1", 0.5), _fill("execution:e-2", 0.5)],
        activity=None,
        budget=None,
        exit_in_progress=False,
    )

    assert facts.trade_count == 2


# ── how a simulated fill was priced (#2912) ─────────────────────────────

_MINUTE_MS = 60_000


def _sale(order_ref: str = "alpaca/exit-1", *, filled_at_ms: int = _START) -> FillRecord:
    return FillRecord(
        account_id="sim:spy-bot", sid="spy-bot", intent_id="exit-1", order_ref=order_ref, event_key=f"fill:{order_ref}",
        symbol="QQQ", side=OrderSide.SELL, quantity=1.0, fill_price=512.4, filled_at_ms=filled_at_ms, fee=None,
    )


def _priced_by(fill_model: str = "decision_bar_close", *, provider: str = "ibkr", bar_end_ms: int = _START) -> SynthesizedAnchor:
    return SynthesizedAnchor(
        fill_model=fill_model, evidence_account_id="sim:spy-bot", provider=provider, bar_identity="QQQ:1m",
        bar_ref="bar-1", decision_bar_start_ms=bar_end_ms - _MINUTE_MS, decision_bar_end_ms=bar_end_ms,
    )


@pytest.mark.parametrize(
    ("anchor", "kind", "caution"),
    [
        (_priced_by(), "bar_close", False),
        (_priced_by("limit_touch"), "limit_touch", False),
        (_priced_by(provider=RECOVERY_QUOTE_PROVIDER), "live_quote", False),
        (_priced_by(provider=RUN_END_CLOSE_PROVIDER), "run_end_close", True),
        (_priced_by("older_bar_close", bar_end_ms=_START - 3 * _MINUTE_MS), "older_bar_close", True),
    ],
)
def test_a_simulated_fill_says_which_rule_priced_it(anchor: SynthesizedAnchor, kind: str, caution: bool) -> None:
    """A run-end close's and a recovery exit's own prices are told from a market bar's by who made them."""
    [listed] = run_fill_views([_sale()], {"alpaca/exit-1": anchor})

    assert listed.simulated is not None
    assert (listed.simulated.kind, listed.simulated.caution) == (kind, caution)


def test_a_sale_at_an_older_price_carries_that_prices_age() -> None:
    """#2912 story 27: the owner sees how old the price was, so they know how far to trust the fill."""
    three_minutes = 3 * _MINUTE_MS
    anchor = _priced_by("older_bar_close", bar_end_ms=_START - three_minutes)

    [listed] = run_fill_views([_sale()], {"alpaca/exit-1": anchor})

    assert listed.simulated is not None and listed.simulated.price_age_ms == three_minutes
    assert duration_label(three_minutes) in listed.simulated.label
    assert duration_label(three_minutes) in listed.simulated.sentence


def test_a_run_end_close_carries_the_age_of_the_last_price_its_run_saw() -> None:
    """The close is stamped when it sells, overnight if need be; its record keeps when that price last traded."""
    overnight = 9 * 60 * _MINUTE_MS
    restated = _priced_by(provider=RUN_END_CLOSE_PROVIDER).model_copy(update={"restated_bar_end_ms": _START - overnight})

    [listed] = run_fill_views([_sale()], {"alpaca/exit-1": restated})

    assert listed.simulated is not None
    assert (listed.simulated.kind, listed.simulated.price_age_ms) == ("run_end_close", overnight)
    assert duration_label(overnight) in listed.simulated.label


def test_a_price_dated_after_its_fill_by_another_clock_has_no_age_and_never_a_negative_one() -> None:
    """The price's time is the feed's and the fill's is this process's clock, which can run behind."""
    ahead = _priced_by(provider=RUN_END_CLOSE_PROVIDER).model_copy(update={"restated_bar_end_ms": _START + 5_000})

    [listed] = run_fill_views([_sale()], {"alpaca/exit-1": ahead})

    assert listed.simulated is not None and listed.simulated.price_age_ms == 0


@pytest.mark.parametrize("anchor", [_priced_by(), _priced_by(provider=RUN_END_CLOSE_PROVIDER)])
def test_a_fill_claims_an_age_only_when_its_record_holds_one(anchor: SynthesizedAnchor) -> None:
    """A price of its own minute has none, and a run-end close recorded before the time was kept does not guess one."""
    [listed] = run_fill_views([_sale()], {"alpaca/exit-1": anchor})

    assert listed.simulated is not None and listed.simulated.price_age_ms is None


@pytest.mark.parametrize(("side", "quoted"), [(OrderSide.SELL, "bid"), (OrderSide.BUY, "ask")])
def test_a_recovery_exit_names_the_side_of_the_quote_it_filled_at(side: OrderSide, quoted: str) -> None:
    """A sale fills at the bid and a buy to cover at the ask; the page never names the wrong one."""
    recovery = FillRecord(
        account_id="sim:spy-bot", sid="spy-bot", intent_id="exit-1", order_ref="alpaca/exit-1", event_key="fill:1",
        symbol="QQQ", side=side, quantity=1.0, fill_price=512.4, filled_at_ms=_START, fee=None,
    )

    [listed] = run_fill_views([recovery], {"alpaca/exit-1": _priced_by(provider=RECOVERY_QUOTE_PROVIDER)})

    assert listed.simulated is not None
    assert set(re.findall(r"\b(?:bid|ask)\b", listed.simulated.sentence)) == {quoted}


def test_a_fill_alpaca_made_is_not_called_simulated() -> None:
    """A Paper or Live authority keeps no such record, and none of its fills is a simulated one."""
    [listed] = run_fill_views([_sale()], None)

    assert listed.simulated is None


def test_a_simulated_fill_with_no_record_of_its_pricing_says_so() -> None:
    """Every fill of a no-submit authority is simulated: one with no record never passes for a fill Alpaca made."""
    priced = {"alpaca/enter-1": _priced_by()}

    listed = run_fill_views([_sale("alpaca/enter-1"), _sale("alpaca/exit-1")], priced)

    assert [(fill.simulated.kind, fill.simulated.caution) for fill in listed if fill.simulated is not None] == [
        ("bar_close", False),
        ("unknown", True),
    ]


@pytest.mark.parametrize("unreadable", [RuntimeError("corrupt"), OSError("gone"), ValueError("not a record")])
def test_an_order_ledger_that_cannot_be_read_does_not_take_the_bot_page_down(unreadable: Exception) -> None:
    """The fills are the Clerk's and still list; only how each was priced is lost, and each says so."""

    def read() -> dict[str, SynthesizedAnchor]:
        raise unreadable

    no_submit = SimpleNamespace(binds_decision_bar=True, synthesized_fill_anchors=read)

    anchors = panel_data_source._simulated_fill_anchors(no_submit, "spy-bot")

    assert anchors == {}
    assert [fill.simulated.kind for fill in run_fill_views([_sale()], anchors) if fill.simulated] == ["unknown"]


def test_an_authority_whose_fills_alpaca_makes_is_never_asked_for_a_simulated_record() -> None:
    vendor = SimpleNamespace(
        binds_decision_bar=False, synthesized_fill_anchors=lambda: pytest.fail("Alpaca made these fills"),
    )

    assert panel_data_source._simulated_fill_anchors(vendor, "spy-bot") is None
