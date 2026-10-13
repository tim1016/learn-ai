"""The account's picture as its money read serves it, over the Clerk's real books (#3101).

What is counted is the Clerk's own: bots registered and budgeted as a Paper account's are, option fills folded
through the custody folds, a manual buy recorded and not yet in the cash. Nothing was sent to Alpaca, and a real
account keeps no option quote, so what getting out costs is unknown here and says so.

The put spread is bot ``a``'s: the SPY 2026-12-18 505 put sold at $5.00 and the 500 put bought at $2.00. It holds
a requirement of $500.00 ((505 - 500) · 100) and was paid $300.00 net, so bot ``a``'s commitment is
$500.00 collateral less the $300.00 credit: $200.00, and of its $500.00 budget $300.00 is free. Bot ``b`` has
$500.00 free. The account's cash is $2,300.00: its $2,000.00 and the credit.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.contract_terms import ContractTerms
from app.broker.alpaca.clerk.sqlite.facts import ExecutionSliceFilledFacts
from app.broker.alpaca.clerk.sqlite.manual_orders import accept_manual_order
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_acknowledgement
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerOrderLeg
from app.options.contract import OptionContract
from app.schemas.deployment_budget import AccountPictureView
from app.services.broker_v2_panel import exit_estimate_read
from app.services.broker_v2_panel.account_picture_read import read_account_picture
from tests.broker.alpaca.clerk.sqlite.conftest import NOON
from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import RUN_ID, accept_option_order, fill
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import _deploy, _new_budget_repo
from tests.broker.alpaca.clerk.sqlite.test_manual_orders import LEG_ID, OPERATOR_ID, TICKET_ID, filled_order
from tests.broker.alpaca.clerk.sqlite.test_option_event_money import books as _budgeted_books

books = _budgeted_books

DEC_18 = date(2026, 12, 18)
SOLD = OptionContract.of(root="SPY", expiry_date=DEC_18, right="put", strike=505).occ_symbol
BOUGHT = OptionContract.of(root="SPY", expiry_date=DEC_18, right="put", strike=500).occ_symbol
DEC_18_MS = OptionContract.from_occ_symbol(SOLD).expiry_ms


def _opens_a_credit_spread(repo: ClerkSqliteRepository, sid: str = "a") -> None:
    """Bot ``sid`` sells the 505 put at $5.00 and buys the 500 put at $2.00: $300.00 comes in."""
    for side, symbol, price in (("sell", SOLD, "5.00"), ("buy", BOUGHT, "2.00")):
        order = accept_option_order(
            repo, terms=ContractTerms(symbol=symbol, multiplier=100), side=side, contracts=1,
            decision_id=f"{sid}-{symbol}", strategy_instance_id=sid, reference_price=price,
        )
        fill(
            repo, order, execution_id=f"x-{sid}-{symbol}", symbol=symbol, side=side.upper(), quantity=1,
            price=float(price), occurred_at_ms=repo.clock(), fee=0,
        )
        repo.clock.advance(1_000)  # type: ignore[attr-defined]


def _picture(
    repo: ClerkSqliteRepository, *, cash: str, world: str = "real_paper", equity: D | None = None,
) -> AccountPictureView:
    money = repo.account_money(cash=cash, seen_before_ms=repo.clock() + 1)
    return read_account_picture(
        repo, money, world=world, observed_at_ms=repo.clock(), equity=D(cash) if equity is None else equity,  # type: ignore[arg-type]
    )


def _lines(picture: AccountPictureView) -> dict[str, tuple[str | None, list[tuple[str, str]]]]:
    return {
        line.kind: (line.figure.amount_usd, [(term.label, term.amount_usd) for term in line.figure.terms])
        for line in picture.spoken_for.lines
    }


def _cents(usd: str) -> int:
    return int((D(usd) * 100).to_integral_exact())


def _adds_up(picture: AccountPictureView) -> None:
    cash = picture.owned.broker_cash.amount_usd
    assert cash is not None
    assert sum(_cents(line.figure.amount_usd or "0") for line in picture.spoken_for.lines) == _cents(cash)


@pytest.fixture(autouse=True)
def _no_live_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real account keeps no option quote, and no supervisor in this process holds one."""
    monkeypatch.setattr(exit_estimate_read, "get_option_quotes", lambda: None)


def test_a_credit_spread_is_collateral_by_bot_by_underlying_and_expiry_and_an_obligation_apart_from_its_loss(
    books: ClerkSqliteRepository,
) -> None:
    _opens_a_credit_spread(books)

    picture = _picture(books, cash="2300")

    assert (picture.owned.broker_cash.amount_usd, picture.owned.marked_equity.amount_usd) == ("2300.00", "2300.00")
    assert picture.owned.broker_cash.source == "Alpaca's account answer"
    assert _lines(picture) == {
        "bot_free": ("800.00", [("Bot a", "300.00"), ("Bot b", "500.00")]),
        "collateral": ("500.00", [("Bot a", "500.00")]),
        "orders": ("0.00", []),
        "outside": ("0.00", []),
        "fees": ("0.00", []),
        "unreserved": ("1000.00", [
            ("Cash as the account reports it", "2300.00"), ("Set aside for running bots", "-800.00"),
            ("Collateral held against sold options", "-500.00"),
        ]),
    }
    _adds_up(picture)
    concentration = picture.concentration
    assert [(g.underlying, g.expiry_ms, g.collateral_usd) for g in concentration.groups] == [("SPY", DEC_18_MS, "500.00")]
    assert concentration.account_collateral.amount_usd == "500.00"
    # The sold 505 put can oblige 100 shares at 505: $50,500.00, whatever the spread can lose.
    assert picture.obligations.assignment.amount_usd == "50500.00"
    assert [(t.label, t.amount_usd) for t in picture.obligations.assignment.terms] == [("SPY", "50500.00")]
    # No live quote is held for either leg, so the cost to close is unknown, and one bot is why.
    close = picture.obligations.close_everything
    assert (close.state, close.amount_usd) == ("unknown", None)
    assert close.source.startswith("1 of 1 bots' exit estimates cannot be priced")
    assert (picture.obligations.bots_holding, picture.obligations.bots_unpriced) == (1, 1)


def test_a_stopped_bots_collateral_stays_spoken_for_and_appears_by_name(books: ClerkSqliteRepository) -> None:
    """Stopping bot ``a`` releases its $300.00 free budget and nothing it holds: its $500.00 collateral is still
    the account's, under its name, and the lines still add up to the cash."""
    _opens_a_credit_spread(books)
    submit_stop_run(
        books, account_id=books.account_id, strategy_instance_id="a", lifecycle_run_id=RUN_ID,
        operator_reason="owner stopped", clock=books.clock,
    )

    picture = _picture(books, cash="2300")

    assert _lines(picture) == {
        "bot_free": ("500.00", [("Bot b", "500.00")]),
        "collateral": ("500.00", [("Stopped bot a", "500.00")]),
        "orders": ("0.00", []),
        "outside": ("0.00", []),
        "fees": ("0.00", []),
        "unreserved": ("1300.00", [
            ("Cash as the account reports it", "2300.00"), ("Set aside for running bots", "-500.00"),
            ("Collateral held against sold options", "-500.00"),
        ]),
    }
    _adds_up(picture)
    # Its position is still held, so its obligation and its cost to close are still the account's.
    assert picture.obligations.assignment.amount_usd == "50500.00"
    assert picture.obligations.close_everything.source.startswith("1 of 1 bots' exit estimates")
    assert "Stopped bot a" in picture.obligations.close_everything.source


def test_a_bot_whose_estimate_cannot_be_worked_out_is_unknown_and_the_partition_survives(
    books: ClerkSqliteRepository, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """One bot's legs that cannot be priced are that bot's unknown estimate, logged: they never take the cash, what
    is spoken for or the collateral down with them."""
    _opens_a_credit_spread(books)

    def unworkable(*args: object, **kwargs: object) -> None:
        raise ValueError("the multiplier of the sold put is a whole number, one or more; got 0")

    monkeypatch.setattr(exit_estimate_read, "exit_estimate", unworkable)

    picture = _picture(books, cash="2300")

    assert picture.spoken_for.partition.state == "known" and picture.owned.broker_cash.amount_usd == "2300.00"
    _adds_up(picture)
    assert picture.concentration.account_collateral.amount_usd == "500.00"
    close = picture.obligations.close_everything
    assert (close.state, close.amount_usd, picture.obligations.bots_unpriced) == ("unknown", None, 1)
    assert "This bot's exit estimate could not be worked out: the multiplier of the sold put" in close.source
    assert [r.__dict__.get("action") for r in caplog.records if r.levelname == "WARNING"] == ["exit_estimate_unworkable"]


def test_what_cannot_be_added_up_to_the_cash_is_logged_as_an_error_as_well_as_said(
    books: ClerkSqliteRepository, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from dataclasses import replace

    from app.broker.alpaca.clerk.account_picture import account_picture as real
    from app.services.broker_v2_panel import account_picture_read

    _opens_a_credit_spread(books)

    def with_a_claim_dropped(budget: object, *args: object, **kwargs: object) -> object:
        return real(replace(budget, available=budget.available + 1000), *args, **kwargs)  # type: ignore[attr-defined,type-var]

    monkeypatch.setattr(account_picture_read, "account_picture", with_a_claim_dropped)

    picture = _picture(books, cash="2300")

    assert picture.spoken_for.partition.state == "unknown" and picture.owned.broker_cash.amount_usd == "2300.00"
    errors = [record.__dict__.get("action") for record in caplog.records if record.levelname == "ERROR"]
    assert errors.count("account_picture_partition_unknown") == 1


def test_a_world_that_is_not_known_fails_loudly_and_is_never_taken_for_a_real_account(
    books: ClerkSqliteRepository,
) -> None:
    with pytest.raises(KeyError):
        _picture(books, cash="1000", world="a_new_kind_of_authority")


def test_a_dry_runs_picture_says_its_cash_and_equity_are_simulated(books: ClerkSqliteRepository) -> None:
    picture = _picture(books, cash="1000", world="synthetic", equity=D("1010.50"))

    assert (picture.owned.broker_cash.amount_usd, picture.owned.marked_equity.amount_usd) == ("1000.00", "1010.50")
    for figure in (picture.owned.broker_cash, picture.owned.marked_equity):
        assert "Dry Run" in figure.source and "simulated" in figure.source


def test_a_holding_the_books_cannot_count_in_whole_contracts_leaves_the_cash_known_and_the_rest_unknown(
    books: ClerkSqliteRepository,
) -> None:
    """A position of half a contract is the books being wrong: nothing is rounded, the figures that stand on it
    are unknown with the reason, and what stands on the cash is still told."""
    with books.write_fence() as conn:
        conn.execute(
            "INSERT INTO option_contracts (symbol, multiplier, recorded_at_ms) VALUES (?, 100, ?)", (SOLD, NOON),
        )
        subject = conn.execute("SELECT subject_id FROM custody_subjects WHERE strategy_instance_id = 'a'").fetchone()[0]
        conn.execute(
            "INSERT INTO positions (subject_id, strategy_instance_id, symbol, attributed_qty, updated_at_ms) "
            "VALUES (?, 'a', ?, -0.5, ?)",
            (subject, SOLD, NOON),
        )

    picture = _picture(books, cash="2300")

    assert picture.owned.broker_cash.amount_usd == "2300.00" and picture.spoken_for.partition.state == "known"
    for figure in (picture.concentration.account_collateral, picture.obligations.assignment, picture.obligations.close_everything):
        assert (figure.state, figure.amount_usd) == ("unknown", None)
        assert "held whole" in figure.source


def test_a_manual_buy_the_cash_has_not_shown_yet_is_outside_the_system_and_still_adds_up(tmp_path: Path) -> None:
    """Bot ``a`` has $400.00 free. 6 SPY were bought by hand at $100 with a $0.03 fee, and the cash reading of
    $2,000.00 was taken before it: $600.03 is claimed, and $2,000.00 - $400.00 - $600.03 = $999.97 is unreserved."""
    repo = _new_budget_repo(tmp_path)
    try:
        _deploy(repo, "a", 40_000)
        accepted = accept_manual_order(
            repo, account_id=repo.account_id, operator_id=OPERATOR_ID, ticket_id=TICKET_ID,
            leg_id=LEG_ID, leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=6),
        )
        leg = accepted.leg
        assert leg.order_ref and leg.effect_operation_id
        repo.append_transition(TransitionInput(
            command_id=accepted.command.command_id, effect_operation_id=leg.effect_operation_id,
            order_ref=leg.order_ref, transition_kind="EXECUTION_SLICE_FILLED",
            custody_owner="ACCOUNT_CLERK", execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress", source_event_at_ms=NOON, clerk_observed_at_ms=NOON,
            summary_code="EXECUTION_SLICE_FILLED", facts_json=ExecutionSliceFilledFacts(
                execution_id="manual-exact", symbol="SPY", side="BUY", slice_qty=6, slice_price=100, fee=.03,
                fee_fidelity="reported", evidence_source="websocket", source_event_at_ms=NOON,
            ).to_facts_json(),
        ))
        fold_order_acknowledgement(repo, effect_operation_id=leg.effect_operation_id, order=filled_order(leg.order_ref).model_copy(update={
            "quantity": 6, "filled_quantity": 6, "filled_avg_price": 100, "updated_at_ms": NOON, "observed_at_ms": NOON,
        }))
        money = repo.account_money(cash="2000", seen_before_ms=NOON)

        picture = read_account_picture(repo, money, world="real_paper", observed_at_ms=NOON, equity=D("2000"))

        assert _lines(picture) == {
            "bot_free": ("400.00", [("Bot a", "400.00")]),
            "collateral": ("0.00", []),
            "orders": ("0.00", []),
            "outside": ("600.03", [("Manual trades and outside orders", "600.03")]),
            "fees": ("0.00", []),
            "unreserved": ("999.97", [
                ("Cash as the account reports it", "2000.00"), ("Set aside for running bots", "-400.00"),
                ("Outside purchases not in the cash yet", "-600.03"),
            ]),
        }
        _adds_up(picture)
        # Nothing is held in options: nothing to be obliged to, and nothing to close.
        assert picture.obligations.assignment.amount_usd == "0.00"
        assert (picture.obligations.close_everything.amount_usd, picture.obligations.bots_holding) == ("0.00", 0)
    finally:
        repo.close()
