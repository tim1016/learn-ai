"""The IV builder over a pinned provider chain: no underlying fetch, typed failures, named sources (#3524, #3527; R25, T18).

The cross-sectional study's option inputs stay with the provider. These tests run the real wrapper
(``PolygonClientService``), contract finder, IV builder and solver over ``tests/_helpers/option_chain.OptionChain``,
installed at the SDK client's seam. The chain is priced with a known volatility, so what the builder returns can be
checked against the number that generated it.

The windows are short on purpose. ``contract_finder`` looks expiries up once per month and reuses the bracket for the
whole month, while ``iv_builder._interpolate_iv`` (since #3484) raises when 30 days is no longer inside a day's
bracket; the chain's two expiries are far enough apart that the first fortnight of a month stays bracketed. A longer
window raises ``ValueError``; that interaction is recorded in ``docs/known-gaps.md`` and is not changed here.
"""

from __future__ import annotations

import logging
from datetime import date

import numpy as np
import pytest

from app.config import settings
from app.research.options import iv_builder
from app.research.options.iv_builder import build_iv_history
from app.research.options.option_input_ledger import RATE_DEFAULT, RATE_FRED, OptionInputLedger, combine
from app.services.polygon_client import PolygonClientService
from app.services.risk_free_rate import DEFAULT_RISK_FREE_RATE
from tests._helpers.option_chain import OptionChain, option_ticker, parse_option_ticker

#: The first two weeks of March 2024: the month's one expiry lookup (on Mar 1) keeps 30 days inside [low, high].
WINDOW = [date(2024, 3, 1), *(date(2024, 3, d) for d in (4, 5, 6, 7, 8, 11, 12, 13, 14, 15))]
LOW_EXPIRY, HIGH_EXPIRY = date(2024, 3, 29), date(2024, 4, 15)

#: Option prices are rounded to 1e-4; vega is at least 5 per unit of volatility for these 14-45 day options on a
#: $150 underlying, so a half-ten-thousandth of price moves an implied volatility by at most 1e-5. A tenth of a
#: point of volatility is ten times that.
IV_ATOL = 1e-4


def stock_rows(days: list[date], *, start: float = 150.0, step: float = 0.37) -> list[dict]:
    """Daily stock rows as the study hands them over: a gentle walk, timestamps at the session open."""
    from app.lean_sidecar.trading_calendar import session_open_ms_utc

    return [
        {"timestamp": session_open_ms_utc(day), "open": start + step * i, "high": start + step * i + 1.0,
         "low": start + step * i - 1.0, "close": round(start + step * i, 4), "volume": 1_000_000.0}
        for i, day in enumerate(days)
    ]


@pytest.fixture(autouse=True)
def no_fred(monkeypatch: pytest.MonkeyPatch) -> None:
    """FRED is not reachable from a test: the rate is the one default, and the ledger says so."""
    monkeypatch.setattr(settings, "FRED_API_KEY", "", raising=False)


def build(chain: OptionChain, rows: list[dict], ledger: OptionInputLedger, *, days: list[date] = WINDOW):
    service = PolygonClientService()
    service.client = chain  # type: ignore[assignment]
    return build_iv_history("SPY", days[0].isoformat(), days[-1].isoformat(), service, rows, ledger)


def chain_for(rows: list[dict], days: list[date] = WINDOW, **options) -> OptionChain:
    spots = {day: row["close"] for day, row in zip(days, rows, strict=True)}
    return OptionChain("SPY", spots, rate=DEFAULT_RISK_FREE_RATE, expiries=(LOW_EXPIRY, HIGH_EXPIRY), **options)


# ---------------------------------------------------------------------------
# The numbers: the builder recovers the volatility the chain was priced with
# ---------------------------------------------------------------------------


def test_the_iv_history_recovers_the_volatility_the_chain_was_priced_with():
    """Both brackets are priced at the day's volatility, so the 30-day interpolation must return it."""
    rows = stock_rows(WINDOW)
    chain = chain_for(rows)

    frame = build(chain, rows, OptionInputLedger())

    assert list(frame["date"]) == [day.isoformat() for day in WINDOW]
    expected = [chain.sigma(day) for day in WINDOW]
    np.testing.assert_allclose(frame["iv_30d_atm"].to_numpy(dtype=float), expected, atol=IV_ATOL, rtol=0)
    np.testing.assert_allclose(frame["iv_30d_put"].to_numpy(dtype=float), expected, atol=IV_ATOL, rtol=0)
    np.testing.assert_allclose(frame["iv_30d_call"].to_numpy(dtype=float), expected, atol=IV_ATOL, rtol=0)
    assert frame["stock_close"].tolist() == [row["close"] for row in rows], "the spot is the stock bar the study passed in"


def test_a_different_stock_close_picks_a_different_atm_contract():
    """The stock close sets the ATM strike, so the session the close comes from can change the contracts priced (D02)."""
    low_rows, high_rows = stock_rows(WINDOW[:1], start=150.0), stock_rows(WINDOW[:1], start=150.9)
    chain_low, chain_high = chain_for(low_rows, WINDOW[:1]), chain_for(high_rows, WINDOW[:1])
    ledger_low, ledger_high = OptionInputLedger(), OptionInputLedger()

    build(chain_low, low_rows, ledger_low, days=WINDOW[:1])
    build(chain_high, high_rows, ledger_high, days=WINDOW[:1])

    strikes = lambda chain: {parse_option_ticker(t)[3] for t in chain.bar_requests}  # noqa: E731
    # 150.0 and 150.9 sit on either side of the 150.5 midpoint between the $150 and $151 strikes.
    assert 150.0 in strikes(chain_low) and 150.0 not in strikes(chain_high)
    assert 151.0 in strikes(chain_high) and 151.0 not in strikes(chain_low)
    assert ledger_low.observation_digest("option_contracts") != ledger_high.observation_digest("option_contracts")
    assert ledger_low.observation_digest("option_bars") != ledger_high.observation_digest("option_bars")


# ---------------------------------------------------------------------------
# The bypass is gone: the underlying is never fetched from the provider
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", [None, []], ids=["none", "empty"])
def test_without_stock_bars_the_builder_returns_nothing_and_asks_the_provider_for_nothing(missing):
    """Before #3524 ``stock_bars=None`` quietly fetched the underlying's daily bars from Polygon."""
    rows = stock_rows(WINDOW)
    chain = chain_for(rows)
    ledger = OptionInputLedger()

    frame = build(chain, missing, ledger)  # type: ignore[arg-type]

    assert frame.empty
    assert chain.contract_requests == [] and chain.bar_requests == []
    assert ledger.as_dict()["failures_total"] == 0


def test_the_underlying_is_never_in_a_provider_request_during_a_build():
    rows = stock_rows(WINDOW)
    chain = chain_for(rows)

    build(chain, rows, OptionInputLedger())

    assert chain.bar_requests and all(t.startswith("O:") for t in chain.bar_requests)


# ---------------------------------------------------------------------------
# #3527: a provider request that fails is typed and visible, not a quiet missing bar
# ---------------------------------------------------------------------------


def test_an_option_bar_fetch_that_fails_is_recorded_with_its_reason_and_the_rest_still_build(caplog):
    """Before: logged at debug, read as "that contract did not trade", visible nowhere in the result."""
    rows = stock_rows(WINDOW)
    boom = ConnectionError("Max retries exceeded with url: /v2/aggs/ticker/x?apiKey=SECRET123&limit=1")

    def fail_high_puts(ticker: str) -> Exception | None:
        _, expiry, kind, _ = parse_option_ticker(ticker)
        return boom if (expiry, kind) == (HIGH_EXPIRY, "put") else None

    chain = chain_for(rows, bar_failure=fail_high_puts)
    ledger = OptionInputLedger()

    with caplog.at_level(logging.WARNING, logger=iv_builder.logger.name):
        frame = build(chain, rows, ledger)

    asked_high_puts = {t for t in chain.bar_requests if parse_option_ticker(t)[1:3] == (HIGH_EXPIRY, "put")}
    assert asked_high_puts, "the chain listed puts at the far expiry and the builder asked for them"
    failures = ledger.failures
    assert {f.key for f in failures} == asked_high_puts and all(f.stage == "option_bars" for f in failures)
    assert {f.error_type for f in failures} == {"ConnectionError"}
    assert all("SECRET123" not in f.reason and "apiKey=REDACTED" in f.reason for f in failures), "a key never reaches a result"
    block = ledger.as_dict()
    assert block["failures_total"] == len(asked_high_puts)
    assert block["inputs"]["option_bars"]["failed"] == len(asked_high_puts)
    assert block["inputs"]["option_bars"]["attempted"] == len(chain.bar_requests)
    assert any(r.getMessage().startswith("option bars were not fetched") for r in caplog.records), "and it is logged above debug"
    # The failed contracts are the far-expiry puts: the skew leg that needs them is absent, never filled; ATM survives.
    assert frame["iv_30d_put"].isna().all()
    np.testing.assert_allclose(frame["iv_30d_atm"].to_numpy(dtype=float), [chain.sigma(d) for d in WINDOW], atol=IV_ATOL, rtol=0)


def test_an_expiry_lookup_that_fails_leaves_no_iv_and_names_the_month():
    rows = stock_rows(WINDOW)
    chain = chain_for(rows, contract_failure=lambda expiry: TimeoutError("read timed out") if expiry is None else None)
    ledger = OptionInputLedger()

    frame = build(chain, rows, ledger)

    assert frame["iv_30d_atm"].isna().all(), "no expiry, no contract, no IV; nothing is substituted"
    assert [(f.stage, f.key, f.error_type) for f in ledger.failures] == [("option_expiries", "SPY:2024-03", "TimeoutError")]
    assert ledger.attempted("option_contracts") == 0


def test_a_contract_lookup_that_fails_is_recorded_per_attempt_and_names_the_expiry():
    rows = stock_rows(WINDOW)
    chain = chain_for(rows, contract_failure=lambda expiry: OSError("reset") if expiry == HIGH_EXPIRY.isoformat() else None)
    ledger = OptionInputLedger()

    frame = build(chain, rows, ledger)

    assert {(f.stage, f.key) for f in ledger.failures} == {("option_contracts", f"SPY:{HIGH_EXPIRY.isoformat()}")}
    assert ledger.failed_count("option_contracts") == len(WINDOW), "a failed expiry is asked again on every date, and each ask is counted"
    assert frame["iv_30d_atm"].isna().all(), "the far bracket is missing, so no 30-day interpolation exists"


# ---------------------------------------------------------------------------
# R25: every option input is named, fingerprinted, and says it cannot be reproduced
# ---------------------------------------------------------------------------


def test_the_ledger_names_each_provider_input_and_fingerprints_what_it_answered():
    rows = stock_rows(WINDOW)
    chain = chain_for(rows)
    ledger = OptionInputLedger()

    build(chain, rows, ledger)

    block = ledger.as_dict()
    assert set(block["inputs"]) == {"option_expiries", "option_contracts", "option_bars"}
    for stage, entry in block["inputs"].items():
        assert entry["kind"] == "provider" and entry["provider"] == "polygon" and entry["reproducible"] is False, stage
        assert entry["attempted"] > 0 and entry["failed"] == 0 and len(entry["observation_digest"]) == 64, stage
    assert block["inputs"]["option_expiries"]["attempted"] == 1, "one lookup for the month"
    assert block["inputs"]["option_contracts"]["attempted"] == 2, "one per bracketing expiry"
    assert block["risk_free_rate"] == {
        "kind": "provider", "provider": "fred", "days_from_fred": 0, "days_from_default": len(WINDOW), "reproducible": False,
    }
    assert block["failures"] == [] and block["failures_total"] == 0


def test_a_changed_provider_answer_is_a_changed_observation_digest():
    rows = stock_rows(WINDOW)
    first, second = OptionInputLedger(), OptionInputLedger()
    again = OptionInputLedger()

    build(chain_for(rows), rows, first)
    build(chain_for(rows), rows, again)
    build(chain_for(rows, sigma=lambda day: 0.31), rows, second)

    assert first.observation_digest("option_bars") == again.observation_digest("option_bars"), "the same answers, the same identity"
    assert first.observation_digest("option_bars") != second.observation_digest("option_bars")
    assert first.observation_digest("option_expiries") == second.observation_digest("option_expiries"), "only the bars changed"


def test_the_risk_free_rate_source_is_counted_per_day(monkeypatch):
    rows = stock_rows(WINDOW)
    monkeypatch.setattr(iv_builder, "get_risk_free_rate_and_source", lambda **kwargs: (DEFAULT_RISK_FREE_RATE, RATE_FRED))
    ledger = OptionInputLedger()

    build(chain_for(rows), rows, ledger)

    assert (ledger.rate_days(RATE_FRED), ledger.rate_days(RATE_DEFAULT)) == (len(WINDOW), 0)


def test_combine_sums_the_tickers_and_carries_each_failure_with_its_ticker():
    rows = stock_rows(WINDOW)
    clean, failing = OptionInputLedger(), OptionInputLedger()
    build(chain_for(rows), rows, clean)
    build(chain_for(rows, bar_failure=lambda t: OSError("down") if parse_option_ticker(t)[2] == "put" else None), rows, failing)

    run = combine({"SPY": clean, "QQQ": failing})

    assert run["failures_total"] == failing.failure_count > 0
    assert {f["ticker"] for f in run["failures"]} == {"QQQ"}
    bars = run["inputs"]["option_bars"]
    assert bars["attempted"] == clean.attempted("option_bars") + failing.attempted("option_bars")
    assert bars["failed"] == failing.failure_count
    assert run["risk_free_rate"]["days_from_default"] == 2 * len(WINDOW)
    assert combine({})["inputs"]["option_bars"]["observation_digest"] is None


def test_option_ticker_round_trips_through_its_parser():
    ticker = option_ticker("SPY", date(2024, 3, 29), "put", 150.5)

    assert ticker == "O:SPY240329P00150500"
    assert parse_option_ticker(ticker) == ("SPY", date(2024, 3, 29), "put", 150.5)
