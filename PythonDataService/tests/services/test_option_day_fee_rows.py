"""A real account's fee rows on a day that has option fills (#2913).

A posted row is shared among every fill of its day, stock and option alike,
each weighted by what its own fee model predicts. Every figure is derived by
hand from the rates in force on 2026-09-08:

- a share: SEC $0.0000206 a dollar sold, TAF $0.000195 a share sold, CAT
  $0.000003 a share;
- a contract of 100 shares: SEC $0.0000206 a dollar of premium sold, TAF
  $0.00329 a contract sold, CAT $0.000003 a share it stands for, ORF $0.015
  and OCC $0.025 a contract.

The day's fills, and what each one's own model predicts for it::

    bot S  buys 100 shares at $50.00    SEC 0        TAF 0       CAT 0.0003
           sells 100 shares at $50.00   SEC 0.103    TAF 0.0195  CAT 0.0003
           in all 0.1231                SEC 0.103    TAF 0.0195  CAT 0.0006
    bot A  buys 10 calls at $1.94       SEC 0        TAF 0       CAT 0.003   ORF 0.15   OCC 0.25
           sells 10 calls at $2.40      SEC 0.04944  TAF 0.0329  CAT 0.003   ORF 0.15   OCC 0.25
           in all 0.88834               SEC 0.04944  TAF 0.0329  CAT 0.006   ORF 0.30   OCC 0.50
    bot B  buys 1 call at $1.94         SEC 0        TAF 0       CAT 0.0003  ORF 0.015  OCC 0.025
           in all 0.0403

Bot B is in the day only where a test says so. Decimal equality is exact.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.broker.contract.models import OrderSide
from app.services.alpaca_fee_attribution import FeeAttribution, FeeCharge, FeeFill, attribute_session_fees
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms

D = Decimal
DAY = et_midnight_ms(date(2026, 9, 8))
# The label the attribution prints for that day's modelled figures.
DAY_TEXT = et_date_at_ms(DAY).isoformat()
# When the Clerk read the day's fee rows.
READ = 100


def _shares(fill_id: str, subject: str, side: OrderSide, shares: int, price: str) -> FeeFill:
    return FeeFill(fill_id, subject, side, D(shares), D(price), native_order_id=f"order-{fill_id}")


def _contracts(fill_id: str, subject: str, side: OrderSide, contracts: int, premium: str) -> FeeFill:
    return FeeFill(
        fill_id, subject, side, D(contracts), D(premium), native_order_id=f"order-{fill_id}", multiplier=100
    )


BOT_S = [_shares("s-buy", "bot:s", OrderSide.BUY, 100, "50.00"), _shares("s-sell", "bot:s", OrderSide.SELL, 100, "50.00")]
BOT_A = [
    _contracts("a-buy", "bot:a", OrderSide.BUY, 10, "1.94"),
    _contracts("a-sell", "bot:a", OrderSide.SELL, 10, "2.40"),
]
BOT_B = [_contracts("b-buy", "bot:b", OrderSide.BUY, 1, "1.94")]


def _the_day(fills: list[FeeFill], rows: list[FeeCharge] | None = None, **changes: object) -> FeeAttribution:
    """A real account's day, read after it ended with every fill and every activity known."""
    return attribute_session_fees(**{
        "trade_date_ms": DAY, "fills": fills, "charges": rows or [], "population_complete": True,
        "activities_complete": True, "session_ended": True, "settlement_at_ms": 7, **changes,
    })


def _row(key: str, amount: str, **named: object) -> FeeCharge:
    return FeeCharge(key, D(amount), READ, **named)


def _charged(result: FeeAttribution, state: str) -> dict[tuple[str, str], Decimal]:
    """Each share in ``state`` as (what it is filed under, whose it is) -> dollars."""
    return {(share.charge_id, share.subject_id): share.amount for share in result.shares if share.state == state}


def _total(result: FeeAttribution) -> Decimal:
    return sum((share.amount for share in result.shares), D(0))


def test_before_any_fee_row_each_fill_is_charged_its_own_models_estimate() -> None:
    """Each model rounds each of its fee types up to the cent, the stock fills and the option fills apart.

    Bot S: SEC $0.103 is 11 cents, TAF $0.0195 is 2, CAT $0.0006 is 1: $0.14.
    Bot A: SEC $0.04944 is 5 cents, TAF $0.0329 is 4, CAT $0.006 is 1, ORF 30, OCC 50: $0.90.
    """
    estimate = _the_day(BOT_S + BOT_A)

    assert estimate.known, "the day is known"
    assert _charged(estimate, "estimated") == {
        (f"model:{DAY_TEXT}:sec", "bot:s"): D("0.11"), (f"model:{DAY_TEXT}:taf", "bot:s"): D("0.02"),
        (f"model:{DAY_TEXT}:cat", "bot:s"): D("0.01"),
        (f"model:{DAY_TEXT}:option:sec", "bot:a"): D("0.05"), (f"model:{DAY_TEXT}:option:taf", "bot:a"): D("0.04"),
        (f"model:{DAY_TEXT}:option:cat", "bot:a"): D("0.01"), (f"model:{DAY_TEXT}:option:orf", "bot:a"): D("0.30"),
        (f"model:{DAY_TEXT}:option:occ", "bot:a"): D("0.50"),
    }, "each model rounds each of its fee types up to the cent, the stock fills and the option fills apart"
    assert {share.state for share in estimate.shares} == {"estimated"}, "every share is an estimate"
    assert (estimate.predicted_total, estimate.observed_total) == (D("1.04"), None), "predicted $1.04, nothing observed"
    # The shares beside the contracts are charged what they are charged alone.
    assert (
        [share for share in estimate.shares if share.subject_id == "bot:s"] == list(_the_day(BOT_S).shares)
    ), "the shares beside the contracts are charged what they are charged alone"
    # Nothing bounds it: $0.14 of bot S's and $0.90 of bot A's, however late cash is read.
    assert (
        estimate.unobserved_cash_claim(cash_seen_before_ms=10**15) == D("1.04")
    ), "an estimate whose rows never come is held back from cash for a stock fill and an option fill alike"


def test_a_row_that_names_no_fee_type_is_shared_among_every_fill_by_all_of_its_models_types_together() -> None:
    """Alpaca posts $1.03 for the day and the row names no fee type.

    Each bot's weight is all its model predicts: S 0.1231, A 0.88834, 1.01144 together.
    S's part of 103 cents is 103 x 0.1231 / 1.01144 = 12.54, A's is 90.46: 12 and 90
    whole cents, and the odd cent goes to the larger remainder, S's. $0.13 and $0.90,
    $1.03 in all. Had the row been shared among the shares alone, S would owe all of it.
    """
    shared = _the_day(BOT_S + BOT_A, [_row("fee-of-the-day", "1.03")])

    assert shared.known and shared.unattributed == 0
    assert _charged(shared, "observed") == {("fee-of-the-day", "bot:s"): D("0.13"), ("fee-of-the-day", "bot:a"): D("0.90")}
    # The row took the place of every estimate; nothing is charged twice.
    assert {share.state for share in shared.shares} == {"observed"}
    assert _total(shared) == shared.observed_total == D("1.03")


def test_a_row_that_names_its_fee_type_is_shared_by_what_each_fills_model_predicts_for_that_type() -> None:
    """Alpaca posts one row for each fee type, each rounded up once across the whole day.

    SEC $0.16 (0.103 + 0.04944 = 0.15244): S's part is 16 x 0.103 / 0.15244 = 10.81 cents and
        A's 5.19: 10 and 5, and the odd cent is S's. $0.11 and $0.05.
    TAF $0.06 (0.0195 + 0.0329 = 0.0524): 6 x 0.0195 / 0.0524 = 2.23 and 3.77: 2 and 3, and
        the odd cent is A's. $0.02 and $0.04.
    CAT $0.01 (0.0006 + 0.006): S's part is 0.09 of the cent and A's 0.91. It is A's.
    ORF $0.30 and OCC $0.50: the stock model predicts neither, so both are A's whole.
    S owes $0.13 and A $0.90: $1.03, the five rows' total.
    """
    rows = [
        _row("sec-row", "0.16", component="sec"), _row("taf-row", "0.06", component="taf"),
        _row("cat-row", "0.01", component="cat"), _row("orf-row", "0.30", component="orf"),
        _row("occ-row", "0.50", component="occ"),
    ]

    shared = _the_day(BOT_S + BOT_A, rows)

    assert shared.known
    assert _charged(shared, "observed") == {
        ("sec-row", "bot:s"): D("0.11"), ("sec-row", "bot:a"): D("0.05"),
        ("taf-row", "bot:s"): D("0.02"), ("taf-row", "bot:a"): D("0.04"),
        ("cat-row", "bot:a"): D("0.01"),
        ("orf-row", "bot:a"): D("0.30"),
        ("occ-row", "bot:a"): D("0.50"),
    }
    assert {share.state for share in shared.shares} == {"observed"}
    assert (shared.total_for("bot:s"), shared.total_for("bot:a")) == (D("0.13"), D("0.90"))
    assert _total(shared) == shared.observed_total == D("1.03")


def test_a_row_of_a_type_only_the_option_model_predicts_falls_on_the_option_fills_alone() -> None:
    """An ORF row of $0.32 on a day bots S, A and B all traded (A's 0.30 + B's 0.015 = 0.315).

    A's part is 32 x 0.30 / 0.315 = 30.48 cents and B's 1.52: 30 and 1, and the odd cent is
    B's. $0.30 and $0.02; nothing is S's. Every other fee type is still each model's estimate:
    S's $0.14; of the option fills' SEC 5 cents, TAF 4 and CAT 1 (0.0063) all are A's, and of
    their OCC $0.53 (0.525) A's 50.48 is 50 and B's 2.52 takes the odd cent, 3.
    """
    shared = _the_day(BOT_S + BOT_A + BOT_B, [_row("orf-row", "0.32", component="orf")])

    assert shared.known
    assert _charged(shared, "observed") == {("orf-row", "bot:a"): D("0.30"), ("orf-row", "bot:b"): D("0.02")}
    assert _charged(shared, "estimated") == {
        (f"model:{DAY_TEXT}:sec", "bot:s"): D("0.11"), (f"model:{DAY_TEXT}:taf", "bot:s"): D("0.02"),
        (f"model:{DAY_TEXT}:cat", "bot:s"): D("0.01"),
        (f"model:{DAY_TEXT}:option:sec", "bot:a"): D("0.05"), (f"model:{DAY_TEXT}:option:taf", "bot:a"): D("0.04"),
        (f"model:{DAY_TEXT}:option:cat", "bot:a"): D("0.01"),
        (f"model:{DAY_TEXT}:option:occ", "bot:a"): D("0.50"), (f"model:{DAY_TEXT}:option:occ", "bot:b"): D("0.03"),
    }


def test_a_row_of_an_option_only_type_on_a_day_with_no_option_fill_is_charged_to_no_stock_bot() -> None:
    nobodys = _the_day(BOT_S, [_row("orf-row", "0.05", component="orf")])

    # No fill of the day has a model that predicts the fee: the row has no weight to follow.
    assert not nobodys.known and nobodys.unattributed == D("0.05")
    assert nobodys.total_for("bot:s") == D("0.14")
    assert {share.state for share in nobodys.shares} == {"estimated"}


def test_a_day_of_option_fills_alone_shares_its_row_by_the_option_models_weights() -> None:
    """Bot A buys ten calls and bot B one; nothing else trades. Alpaca posts $0.46 for the day.

    A buy owes no SEC fee and no TAF. A's weight is CAT 0.003 + ORF 0.15 + OCC 0.25 = 0.403
    and B's 0.0403. A's part is 46 x 0.403 / 0.4433 = 41.82 cents and B's 4.18: 41 and 4,
    and the odd cent is A's. $0.42 and $0.04.
    """
    shared = _the_day([BOT_A[0], *BOT_B], [_row("fee-of-the-day", "0.46")])

    assert shared.known and shared.unattributed == 0
    assert _charged(shared, "observed") == {("fee-of-the-day", "bot:a"): D("0.42"), ("fee-of-the-day", "bot:b"): D("0.04")}
    assert _total(shared) == shared.observed_total == D("0.46")


def test_a_row_that_names_an_option_order_is_that_orders_bots_whole() -> None:
    """A row of $0.41 that names bot A's buying order is A's, whatever the models weigh.

    It settles that order's fills only. A's selling order keeps its estimate (SEC 5 cents,
    TAF 4, ORF 15, OCC 25: the CAT cent the two orders share is filed with the buy) and
    bot S keeps all of its own.
    """
    shared = _the_day(BOT_S + BOT_A, [_row("a-buy-fees", "0.41", native_order_id="order-a-buy")])

    assert shared.known
    assert _charged(shared, "observed") == {("a-buy-fees", "bot:a"): D("0.41")}
    assert shared.total_for("bot:a") == D("0.41") + D("0.49")
    assert [share for share in shared.shares if share.subject_id == "bot:s"] == list(_the_day(BOT_S).shares)


def test_a_lone_option_fill_reads_resolved_and_stops_claiming_cash_once_cash_is_read_after_its_days_row() -> None:
    """Bot B's one contract: CAT, ORF and OCC, each rounded up, 1 + 2 + 3 cents.

    Before a row is seen the six cents are an estimate and are held back from cash however
    late cash is read. Once the day's row is seen they are that row's, held back only until
    cash is read after it, as a stock fill's are. An option fill alone is no reason for the
    fees to read unresolved, with the day's row or without it.
    """
    estimate = _the_day(BOT_B)
    charged = _the_day(BOT_B, [_row("fee-of-the-day", "0.06")])

    assert estimate.unresolved == (), "an option fill alone does not read unresolved, before any row"
    assert charged.unresolved == (), "an option fill alone does not read unresolved, with its day's row"
    assert (
        estimate.unobserved_cash_claim(cash_seen_before_ms=READ + 1) == D("0.06")
    ), "an estimate is held back from cash read after the read"
    assert (
        estimate.unobserved_cash_claim(cash_seen_before_ms=10**15) == D("0.06")
    ), "an estimate is held back from cash however late cash is read"
    assert (
        charged.unobserved_cash_claim(cash_seen_before_ms=READ) == D("0.06")
    ), "a charged row is held back until cash is read after it"
    assert (
        charged.unobserved_cash_claim(cash_seen_before_ms=READ + 1) == 0
    ), "a charged row is released once cash is read after it"


def test_a_day_whose_fills_are_not_all_known_shares_no_row_and_reads_unresolved() -> None:
    unknown = _the_day(BOT_S + BOT_A, [_row("fee-of-the-day", "1.03")], population_complete=False)

    assert not unknown.known
    assert (unknown.shares, unknown.unattributed) == ((), D("1.03"))


def test_a_row_seen_while_its_day_is_still_open_is_nobodys_until_the_day_ends() -> None:
    still_open = _the_day(BOT_B, [_row("fee-of-the-day", "0.06")], session_ended=False)

    # As for a stock fill: a day's rows are not known to be all in before the day ends.
    assert not still_open.known and still_open.unattributed == D("0.06")
    assert _charged(still_open, "estimated") and not _charged(still_open, "observed")


def test_a_fee_alpaca_reports_on_an_option_fill_itself_is_that_fills_charge_and_is_not_estimated_again() -> None:
    reported = FeeFill("b-buy", "bot:b", OrderSide.BUY, D(1), D("1.94"), reported_fee=D("0.26"), multiplier=100)

    charged = _the_day([reported])

    assert charged.known and charged.total_for("bot:b") == D("0.26")
    assert [(share.state, share.included_in_fill) for share in charged.shares] == [("observed", True)]
    # It is in the fill's own claim on cash, so the fee claim does not count it again.
    assert charged.unobserved_cash_claim(cash_seen_before_ms=0) == 0


def test_an_option_fill_its_model_cannot_price_shares_no_row_and_reads_unresolved() -> None:
    half_a_contract = FeeFill("odd", "bot:b", OrderSide.BUY, D("0.5"), D("1.94"), multiplier=100)

    refused = _the_day([*BOT_S, half_a_contract], [_row("fee-of-the-day", "0.20")])

    assert not refused.known and refused.predicted_total is None
    assert (refused.shares, refused.unattributed) == ((), D("0.20"))


def test_a_day_before_the_option_rates_are_pinned_shares_no_row_and_is_never_free() -> None:
    early = attribute_session_fees(
        trade_date_ms=et_midnight_ms(date(2026, 1, 15)), fills=BOT_B, charges=[_row("fee-of-the-day", "0.06")],
        population_complete=True, activities_complete=True, session_ended=True, settlement_at_ms=7,
    )

    assert not early.known and early.predicted_total is None
    assert (early.shares, early.unattributed) == ((), D("0.06"))
