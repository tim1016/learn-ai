"""The Cross-sectional study's option inputs: the provider path against the lake path, over one pinned payload (#3540 Part B; R14).

Golden fixture ``XSIV-001`` holds what the study's old provider path returned (the vendor's live expiries, contracts and
daily option bars, asked through the Polygon SDK) over a pinned chain: its contract-finder frame and its IV frame. The same
payload is served to the lake path at the vendor's HTTP seam (a listing as of each date, paged; one 15:59 minute per
contract-day whose close is the payload's daily price and whose volume is 500), and the study's real writers, readers,
admission, contract finder and IV builder run over what the lake stored. The provider path is gone from the tree, so the
frozen frames are the comparison; ``tests/fixtures/golden/options-pricing/XSIV-001/v1/attribution.md`` says how they were made.

Every difference is classified against the owner decisions on #3524:

* on the payload itself selection, solver inputs and non-IV fields agree exactly. The three IV columns allow only the
  owner-approved platform roundoff (atol 1e-14, rtol 0); missing-value masks remain exact. ``price_source`` is ``vwap`` on
  the provider path and ``close_filtered`` on the lake path (a *price rule* change: the payload's daily ``vw`` is the 15:59 close);
* each case below is a payload where they would not agree, run on the lake path alone and named for what the provider path
  did instead: ``data-quality`` (the input data differs), ``timestamp`` (an instant is read differently), ``decision`` (a
  different contract is picked) and ``refusal`` (a member the provider path computed on a partial chain is now left out).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest
import respx

from app.config import settings
from app.data_lake.option_listing import read_option_listing
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.options_backtest.fetching import LakeOptionCapture
from app.research.options import iv_builder
from app.research.options.iv_builder import _get_option_price, build_iv_history
from app.research.options.option_inputs import (
    MemberOptionInputs,
    OptionInputRefusal,
    RateTally,
    admit_member_option_inputs,
)
from app.services.risk_free_rate import DEFAULT_RISK_FREE_RATE
from app.utils.session_anchors import et_midnight_ms
from app.volatility.solver import ImpliedVolResult
from tests._helpers.fake_lake_catalog import FakeCatalog, install_fake_catalog, point_lake_writer_at_tmp
from tests._helpers.option_chain import OptionChain, member_inputs, parse_option_ticker, stock_rows
from tests._helpers.option_chain_vendor import ChainVendor, payload_by_ticker
from tests._helpers.option_vendor import bar_row
from tests.fixtures.golden_support.registry import default as registry
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import tmp_lake as tmp_lake

FIXTURE = "XSIV-001"
#: A Monday after the window: every session of it is over in New York and inside the options plan.
NOW_MS = et_midnight_ms(date(2024, 12, 2)) + 12 * 3_600_000
#: Volatility the chain was priced with is recovered to within the price rounding over the thinnest vega (see
#: ``tests/research/options/test_iv_builder_member_inputs.py``); the tolerance is not loosened for the lake path.
IV_ATOL = 1e-5
# Owner-approved precision exception (#3524; ADR 0069): CI's maximum IV drift from the macOS fixture was
# 1.27675647831893e-15. Same-runtime provider/lake replay was exact; one-ULP platform math.log drift was isolated.
# Only final IVs get this bound. Selection, all 336 solver argument tuples, null masks and other fields stay exact.
PROVIDER_IV_ATOL = 1e-14
IV_COLUMNS = ("iv_30d_atm", "iv_30d_put", "iv_30d_call")


def payload() -> dict:
    return json.loads(registry.resolve(FIXTURE, "input").read_text(encoding="utf-8"))


def frozen() -> dict:
    return json.loads(registry.resolve(FIXTURE, "output").read_text(encoding="utf-8"))


def chain_of(data: dict) -> OptionChain:
    table = data["sigma"]
    return OptionChain(
        data["underlying"],
        {date.fromisoformat(day): close for day, close in data["closes"].items()},
        rate=data["rate"],
        sigma=lambda day: table[day.isoformat()],
        strike_step=data["strike_step"],
        expiries=tuple(date.fromisoformat(e) for e in data["expiries"]),
        listed_on={date.fromisoformat(e): date.fromisoformat(d) for e, d in data["listed_on"].items()},
    )


def records(frame: pd.DataFrame) -> list[dict]:
    return [{k: (None if v is None or v != v else v) for k, v in row.items()} for row in frame.to_dict("records")]


@pytest.fixture(autouse=True)
def no_fred(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "FRED_API_KEY", "", raising=False)


@pytest.fixture
def the_vendor(fake_catalog: FakeCatalog, tmp_lake: Path) -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def admit(router: respx.MockRouter, vendor: ChainVendor, data: dict, **options) -> MemberOptionInputs:
    window = [date.fromisoformat(d) for d in data["window"]]
    vendor.mount(router)
    return admit_member_option_inputs(
        data["underlying"],
        spot_rows=data["stock_rows"],
        start_date=window[0],
        end_date=window[-1],
        now_ms=NOW_MS,
        capture=LakeOptionCapture(data["underlying"], now_ms=NOW_MS, should_stop=lambda: False),
        **options,
    )


@dataclass
class ParityWorld:
    """The payload served to the lake path, admitted cold and then again: both are asked of the same lake in turn."""

    data: dict
    expected: dict
    vendor: ChainVendor
    first: MemberOptionInputs
    asked_by_the_first: int
    again: MemberOptionInputs


@pytest.fixture(scope="module")
def parity(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ParityWorld]:
    """One cold admission of the whole payload and one rerun, shared by the cases below (a cold run reads tens of listings)."""
    with pytest.MonkeyPatch.context() as patch:
        install_fake_catalog(patch)
        point_lake_writer_at_tmp(tmp_path_factory.mktemp("parity-lake"), patch)
        patch.setattr(settings, "FRED_API_KEY", "", raising=False)
        data, expected = payload(), frozen()
        vendor = ChainVendor(chain_of(data), price=payload_by_ticker(data["priced_bars"]))
        with respx.mock(assert_all_called=False) as router:
            first = admit(router, vendor, data)
            asked = vendor.requests
            again = admit(router, vendor, data)
        yield ParityWorld(data, expected, vendor, first, asked, again)


# ---------------------------------------------------------------------------
# The payload: exact scientific inputs; final IVs allow only classified platform roundoff
# ---------------------------------------------------------------------------


def test_the_lake_path_selects_the_same_contracts_and_derives_the_same_iv_as_the_frozen_provider_path(
    parity: ParityWorld, monkeypatch: pytest.MonkeyPatch,
) -> None:
    solver_calls: list[dict[str, float | bool]] = []
    solve = iv_builder.implied_volatility

    def record_solve(**kwargs: float | bool) -> ImpliedVolResult:
        solver_calls.append(kwargs)
        return solve(**kwargs)

    monkeypatch.setattr(iv_builder, "implied_volatility", record_solve)
    iv = build_iv_history(parity.data["underlying"], parity.first, RateTally())

    assert records(parity.first.bracket) == parity.expected["bracket"], "selection: every column of the contract finder's frame"
    lake, provider = records(iv), parity.expected["iv"]
    assert len(lake) == len(provider) == len(parity.data["window"])
    assert {row["price_source"] for row in provider} == {"vwap"} and {row["price_source"] for row in lake} == {"close_filtered"}
    unchanged = lambda rows: [{k: v for k, v in row.items() if k not in (*IV_COLUMNS, "price_source")} for row in rows]  # noqa: E731
    assert unchanged(lake) == unchanged(provider), "dates, dte, spot, quality and gaps: exact (==, no tolerance)"
    for column in IV_COLUMNS:
        actual = np.array([row[column] for row in lake], dtype=np.float64)
        expected = np.array([row[column] for row in provider], dtype=np.float64)
        missing = np.isnan(expected)
        np.testing.assert_array_equal(np.isnan(actual), missing, err_msg=f"{column}: exact missing-value mask")
        np.testing.assert_allclose(actual[~missing], expected[~missing], atol=PROVIDER_IV_ATOL, rtol=0, equal_nan=False, err_msg=column)

    # The provider's frozen picks and prices define the solver arguments independently of lake admission (#3524).
    # Compare the multiset because solving one contract does not depend on which other contract was solved first.
    expected_calls = []
    for row in parity.expected["bracket"]:
        for side in ("low", "high"):
            for suffix, is_call in (("atm_contract", True), ("atm_put_contract", False), ("put_contract", False), ("call_contract", True)):
                ticker = row[f"{side}_dte_{suffix}"]
                expected_calls.append({
                    "option_price": parity.data["priced_bars"][ticker][row["date"]],
                    "spot": row["stock_close"],
                    "strike": int(ticker[-8:]) / 1000.0,
                    "ttm": row[f"{side}_dte"] / 365.0,
                    "rate": parity.data["rate"],
                    "is_call": is_call,
                })
    assert len(solver_calls) == len(expected_calls) == 336
    assert Counter(tuple(sorted(call.items())) for call in solver_calls) == Counter(tuple(sorted(call.items())) for call in expected_calls)


def test_the_lake_path_recovers_the_volatility_the_chain_was_priced_with(parity):
    iv = build_iv_history(parity.data["underlying"], parity.first, RateTally())

    expected = [parity.data["sigma"][day] for day in parity.data["window"]]
    for column in ("iv_30d_atm", "iv_30d_put", "iv_30d_call"):
        np.testing.assert_allclose(iv[column].to_numpy(dtype=float), expected, atol=IV_ATOL, rtol=0, err_msg=column)


def test_what_the_lake_path_asked_the_vendor_for_is_what_the_provider_path_priced_and_nothing_else(parity):
    """Provider path: 40 contracts, each asked for over the whole window. Lake path: only the contract-days a session prices."""
    vendor, inputs = parity.vendor, parity.first

    asked_contracts = {f"O:{symbol}" for symbol, _ in vendor.bar_requests}
    assert asked_contracts <= set(parity.expected["contracts_priced"]), "no contract the provider path did not price"
    assert len(vendor.bar_requests) == len(set(vendor.bar_requests)) == inputs.coverage.days_expected
    assert inputs.coverage.days_expected < sum(len(days) for days in parity.data["priced_bars"].values()), "not every contract on every day"
    lists_the_provider_took = {date.fromisoformat(d) for d in parity.expected["expiry_lookups_as_of"]}
    assert lists_the_provider_took <= set(inputs.listings_read), "every list the provider path took, the lake path chose from too"
    assert inputs.coverage.listings_expected == len(parity.data["window"]) == len(vendor.listing_requests), "and a proof for each session"
    for (contract, day), folded in inputs.days.items():
        price = parity.data["priced_bars"].get(contract, {}).get(day.isoformat())
        assert folded.close == (None if price is None else Decimal(repr(price))), (contract, day)
        assert folded.volume == (Decimal(0) if price is None else Decimal(500))


def test_an_identical_rerun_asks_the_vendor_for_nothing_and_returns_the_same_digest_and_frames(parity):
    first, again = parity.first, parity.again

    assert parity.vendor.requests == parity.asked_by_the_first > 0, "a rerun reads the stored files and asks the vendor for nothing"
    assert again.digest() == first.digest()
    assert records(again.bracket) == records(first.bracket)
    assert (again.coverage.listings_added, again.coverage.days_added) == (0, 0)
    assert again.coverage.days_reused == first.coverage.days_expected
    assert first.coverage.listings_added == first.coverage.listings_expected and first.coverage.days_added == first.coverage.days_expected
    assert again.coverage.listings_expected < first.coverage.listings_expected, "a warm run needs no proof listing for a stored day"


# ---------------------------------------------------------------------------
# Classified differences: each changes a scientific input or its admission
# ---------------------------------------------------------------------------

CASE_DAYS = (date(2024, 10, 1), date(2024, 10, 2))
CASE_EXPIRIES = (date(2024, 10, 25), date(2024, 11, 1))


def case(*, strike_step: float = 1.0) -> tuple[dict, OptionChain]:
    """Two sessions that reuse one bracket, with no expiry or month transition to obscure the input change."""
    rows = stock_rows(CASE_DAYS, step=0.0)
    chain = OptionChain(
        "SPY", dict.fromkeys(CASE_DAYS, 150.0), rate=DEFAULT_RISK_FREE_RATE,
        strike_step=strike_step, expiries=CASE_EXPIRIES,
    )
    return {"underlying": "SPY", "window": [day.isoformat() for day in CASE_DAYS], "stock_rows": rows}, chain


def assert_recovers_sigma(inputs: MemberOptionInputs, chain: OptionChain) -> pd.DataFrame:
    frame = build_iv_history("SPY", inputs, RateTally())
    for column in ("iv_30d_atm", "iv_30d_put", "iv_30d_call"):
        np.testing.assert_allclose(
            frame[column].to_numpy(dtype=float), [chain.sigma(day) for day in CASE_DAYS], atol=IV_ATOL, rtol=0,
            err_msg=column,
        )
    return frame


def test_a_last_trade_different_from_the_daily_vwap_changes_iv_without_changing_contracts(the_vendor):
    """D-A, data-quality: keep the frozen daily VWAP; a 20% higher last regular trade is a different IV input."""
    data, expected = payload(), frozen()
    data = {**data, "window": data["window"][:2], "stock_rows": data["stock_rows"][:2]}
    daily_vwap = payload_by_ticker(data["priced_bars"])

    def last_trade(ticker: str, day: date) -> float | None:
        price = daily_vwap(ticker, day)
        return None if price is None else round(price * 1.2, 4)

    inputs = admit(the_vendor, ChainVendor(chain_of(data), price=last_trade), data)
    frame = build_iv_history("SPY", inputs, RateTally())

    assert records(inputs.bracket) == expected["bracket"][:2]
    assert frame["price_source"].tolist() == ["close_filtered", "close_filtered"]
    for column in ("iv_30d_atm", "iv_30d_put", "iv_30d_call"):
        old = np.array([row[column] for row in expected["iv"][:2]])
        assert np.all(frame[column].to_numpy(dtype=float) - old > 0.01), column


def test_more_than_250_contracts_in_the_band_does_not_cut_off_the_puts_or_the_atm_strike(the_vendor):
    """D-B, decision: the old 250-row cap could consume calls before it reached the put side of an expiry."""
    data, chain = case(strike_step=0.1)
    vendor = ChainVendor(chain, page_size=1000)

    inputs = admit(the_vendor, vendor, data)

    for expiry in CASE_EXPIRIES:
        in_band = [row for row in chain.contracts_alive(CASE_DAYS[0]) if row[3] == expiry and 127.5 <= row[2] <= 172.5]
        assert len(in_band) > 250 and all(row[1] == "call" for row in in_band[:250])
    assert vendor.listing_pages > len(vendor.listing_requests), "whole listings cross the vendor's page boundary"
    for prefix in ("low", "high"):
        for suffix in ("atm_contract", "atm_put_contract"):
            assert {parse_option_ticker(ticker)[3] for ticker in inputs.bracket[f"{prefix}_dte_{suffix}"]} == {150.0}
        assert inputs.bracket[f"{prefix}_dte_put_contract"].notna().all(), "the put wing survives the old cap"
    assert_recovers_sigma(inputs, chain)


def test_a_strike_listed_after_first_use_never_enters_that_expirys_kept_picks(the_vendor):
    """D-B, timestamp/decision: a later $150 listing cannot retroactively become the first session's ATM strike."""
    data, chain = case()
    vendor = ChainVendor(chain, unlisted=lambda ticker, day: day == CASE_DAYS[0] and parse_option_ticker(ticker)[3] == 150.0)

    inputs = admit(the_vendor, vendor, data)

    later = read_option_listing("SPY", CASE_DAYS[1])
    assert later is not None and any(option.strike_thousandths == 150_000 for option in later.rows)
    for prefix in ("low", "high"):
        for suffix in ("atm_contract", "atm_put_contract"):
            chosen = inputs.bracket[f"{prefix}_dte_{suffix}"]
            assert len(set(chosen)) == 1, "the owner kept strike reuse within an expiry"
            assert {parse_option_ticker(ticker)[3] for ticker in chosen} == {149.0}
    assert_recovers_sigma(inputs, chain)


@pytest.mark.parametrize("override", [{"shares_per_contract": 50}, {"additional_underlyings": [{"underlying": "CASH", "amount": 1}]}])
def test_the_nearest_strike_is_not_selected_when_its_contract_is_nonstandard(the_vendor, override):
    """D-B, decision: retain the vendor row in the lake, but do not price an adjusted deliverable as 100 shares."""
    data, chain = case()
    vendor = ChainVendor(chain, row_overrides=lambda ticker, day: override if parse_option_ticker(ticker)[3] == 150.0 else None)

    inputs = admit(the_vendor, vendor, data)

    listing = read_option_listing("SPY", CASE_DAYS[0])
    assert listing is not None
    nearest = [option for option in listing.rows if option.strike_thousandths == 150_000]
    assert nearest and all(not option.standard for option in nearest), "nonstandard is stored evidence, not a discarded row"
    for prefix in ("low", "high"):
        for suffix in ("atm_contract", "atm_put_contract"):
            assert {parse_option_ticker(ticker)[3] for ticker in inputs.bracket[f"{prefix}_dte_{suffix}"]} == {149.0}
    assert_recovers_sigma(inputs, chain)


def test_after_close_volume_cannot_make_a_thin_regular_hours_price_pass_the_iv_gate(the_vendor):
    """D-A, data-quality: 49 regular-hours contracts plus 451 after the close passes the old daily input, not the lake rule."""
    data, chain = case()

    def tail(symbol: str, day: date) -> list[dict]:
        price = chain.daily_price(f"O:{symbol}", day)
        assert price is not None
        return [bar_row(0, o=price, h=price, l=price, c=price, v=451, vw=price, n=5, t=session_close_ms_utc(day))]

    inputs = admit(the_vendor, ChainVendor(chain, volume=49, tail=tail), data)
    frame = build_iv_history("SPY", inputs, RateTally())

    assert inputs.days and all(day.volume == Decimal(49) for day in inputs.days.values())
    for ticker, day in inputs.days:
        daily_price = chain.daily_price(ticker, day)
        assert _get_option_price({"vw": daily_price, "c": daily_price, "v": 500}) == (daily_price, "vwap")
        assert _get_option_price(inputs.bar(ticker, day)) == (None, "rejected")
    assert frame[["iv_30d_atm", "iv_30d_put", "iv_30d_call"]].isna().all().all()
    assert frame["iv_quality"].tolist() == ["missing", "missing"]


def test_an_incomplete_second_listing_page_refuses_the_member_instead_of_using_the_first_page(the_vendor):
    """D-G, refusal: an interrupted chain is not an empty/skipped expiry and cannot produce a partial IV history."""
    data, chain = case()

    class BrokenSecondPage(ChainVendor):
        async def _listing(self, request: httpx.Request) -> httpx.Response:
            response = await super()._listing(request)
            if int(request.url.params.get("cursor", 0)) > 0:
                return httpx.Response(401, json={"status": "NOT_AUTHORIZED", "request_id": "second-page"})
            return response

    vendor = BrokenSecondPage(chain, page_size=100)
    with pytest.raises(OptionInputRefusal) as caught:
        admit(the_vendor, vendor, data)

    assert vendor.listing_pages == 2 and vendor.bar_requests == []
    assert caught.value.code == "data_unavailable_from_provider"
    assert [(issue.kind, issue.failure_reason) for issue in caught.value.issues] == [("listing", "provider_auth_error")]
    assert read_option_listing("SPY", CASE_DAYS[0]) is None, "the first page was never published as a whole listing"


def test_raw_spot_recovers_iv_where_split_adjusted_stock_prices_would_select_no_matching_contracts(the_vendor):
    """D-C, data-quality/decision: option strikes stay on the raw basis; a two-for-one adjusted target cannot be the IV spot.

    The job test ``test_the_option_side_is_handed_the_raw_closes_and_the_target_is_measured_on_the_adjusted_ones``
    proves the real stock-admission routing. Here the actual finder/solver demonstrate why that separation matters.
    """
    data, chain = case()
    inputs = admit(the_vendor, ChainVendor(chain), data)
    frame = assert_recovers_sigma(inputs, chain)
    adjusted_rows = [{**row, **{field: row[field] / 2 for field in ("open", "high", "low", "close")}} for row in data["stock_rows"]]
    wrong_basis = member_inputs(chain, adjusted_rows, CASE_DAYS)
    wrong_iv = build_iv_history("SPY", wrong_basis, RateTally())

    assert frame["stock_close"].tolist() == [150.0, 150.0]
    assert wrong_basis.bracket["stock_close"].tolist() == [75.0, 75.0]
    assert wrong_basis.bracket["bracket_gap"].tolist() == ["no_expiries_listed", "no_expiries_listed"]
    assert wrong_iv[["iv_30d_atm", "iv_30d_put", "iv_30d_call"]].isna().all().all()
