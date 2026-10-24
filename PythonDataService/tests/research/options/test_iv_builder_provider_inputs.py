"""The IV builder over a pinned provider chain: no underlying fetch, typed failures, named sources (#3524, #3527; R25, T18).

The cross-sectional study's option inputs stay with the provider. These tests run the real wrapper
(``PolygonClientService``), contract finder, IV builder and solver over ``tests/_helpers/option_chain.OptionChain``,
installed at the SDK client's seam. The chain is priced with a known volatility, so what the builder returns can be
checked against the number that generated it.

The window is two months, as a real run's is. ``contract_finder`` lists a month's expiries in one lookup and picks each
date's pair around 30 days from it (#3528), so the run crosses a month edge and changes pair under it.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import numpy as np
import pytest

from app.config import settings
from app.research.options import contract_finder, iv_builder
from app.research.options.iv_builder import build_iv_history
from app.research.options.option_input_ledger import OptionInputLedger, combine
from app.services import fred_service
from app.services.fred_service import RATE_SOURCE_DEFAULT, RATE_SOURCE_FRED, RateResolution
from app.services.polygon_client import PolygonClientService
from app.services.risk_free_rate import DEFAULT_RISK_FREE_RATE
from tests._helpers.option_chain import OptionChain, option_ticker, parse_option_ticker

#: October and November 2024 up to Thanksgiving: every weekday is a session, so the frame the builder pads to business
#: days holds exactly these rows. One expiry lookup per month (Oct 1, Nov 1) serves the whole window on a chain that
#: lists a Friday every week.
WINDOW = [day for day in (date(2024, 10, 1) + timedelta(days=n) for n in range(58)) if day.weekday() < 5]
#: A slow walk, so a strike chosen at an expiry's first use is still within a few percent of the money when it leaves the pair.
STEP = 0.1

#: Option prices are rounded to 1e-4, so a price is off by at most 5e-5; vega is at least 8.5 per unit of volatility for
#: the 23-36 day options this window prices (ATM 15 to 17, a 25-delta wing 8.5 to 12), so the implied volatility is
#: recovered to within 5e-5 / 8.5 = 5.9e-6. The tolerance stays at 1e-5, the value the short window pinned; it was not
#: loosened to take the longer window. Measured on this payload the largest error is 3.5e-6 (put wing); the solver's own
#: tolerance adds nothing visible.
IV_ATOL = 1e-5


def stock_rows(days: list[date], *, start: float = 150.0, step: float = STEP) -> list[dict]:
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


def expiry_pairs(frame) -> list[tuple[date, date]]:
    """Each row's bracketing expiries, rebuilt from the date and the DTEs the frame carries, not from the ledger."""
    return [
        (date.fromisoformat(row.date) + timedelta(days=int(row.dte_low)), date.fromisoformat(row.date) + timedelta(days=int(row.dte_high)))
        for row in frame.itertuples()
    ]


def chain_for(rows: list[dict], days: list[date] = WINDOW, **options) -> OptionChain:
    spots = {day: row["close"] for day, row in zip(days, rows, strict=True)}
    return OptionChain("SPY", spots, rate=DEFAULT_RISK_FREE_RATE, **options)


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
    """The stock close sets the ATM strike, so the session the close comes from can change the contracts priced."""
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

    def fail_puts(ticker: str) -> Exception | None:
        return boom if parse_option_ticker(ticker)[2] == "put" else None

    chain = chain_for(rows, bar_failure=fail_puts)
    ledger = OptionInputLedger()

    with caplog.at_level(logging.WARNING, logger=iv_builder.logger.name):
        frame = build(chain, rows, ledger)

    asked_puts = {t for t in chain.bar_requests if parse_option_ticker(t)[2] == "put"}
    assert asked_puts, "the chain listed puts and the builder asked for them"
    failures = ledger.failures
    assert {f.key for f in failures} == asked_puts and all(f.stage == "option_bars" for f in failures)
    assert {f.error_type for f in failures} == {"ConnectionError"}
    assert all("SECRET123" not in f.reason and "apiKey=REDACTED" in f.reason for f in failures), "a key never reaches a result"
    block = ledger.as_dict()
    assert block["failures_total"] == len(asked_puts)
    assert block["inputs"]["option_bars"]["failed"] == len(asked_puts)
    assert block["inputs"]["option_bars"]["attempted"] == len(chain.bar_requests)
    assert any(r.getMessage().startswith("option bars were not fetched") for r in caplog.records), "and it is logged above debug"
    # The failed contracts are every put: the skew leg that needs them is absent, never filled; ATM survives on its calls.
    assert frame["iv_30d_put"].isna().all()
    np.testing.assert_allclose(frame["iv_30d_atm"].to_numpy(dtype=float), [chain.sigma(d) for d in WINDOW], atol=IV_ATOL, rtol=0)


def test_an_expiry_lookup_that_fails_leaves_no_iv_and_names_each_lookup_by_its_as_of_date():
    """After a failed first lookup every later session asks again as of its own date (#3528), and each ask is recorded."""
    rows = stock_rows(WINDOW)
    chain = chain_for(rows, contract_failure=lambda expiry: TimeoutError("read timed out") if expiry is None else None)
    ledger = OptionInputLedger()

    frame = build(chain, rows, ledger)

    assert frame["iv_30d_atm"].isna().all(), "no expiry, no contract, no IV; nothing is substituted"
    assert set(frame["missing_reason"]) == {"expiry_lookup_failed"}
    assert [(f.stage, f.key, f.error_type) for f in ledger.failures] == [
        ("option_expiries", f"SPY:{day.isoformat()}", "TimeoutError") for day in WINDOW
    ]
    assert ledger.attempted("option_contracts") == 0


def test_a_re_list_is_a_lookup_of_its_own_recorded_under_its_as_of_date():
    """The first lookup times out once: that session is missing, the next lists again as of itself and the month recovers."""
    rows = stock_rows(WINDOW)
    asked = {"expiry_lookups": 0}

    def first_lookup_times_out(expiry: str | None) -> Exception | None:
        if expiry is not None:
            return None
        asked["expiry_lookups"] += 1
        return TimeoutError("read timed out") if asked["expiry_lookups"] == 1 else None

    chain = chain_for(rows, contract_failure=first_lookup_times_out)
    ledger = OptionInputLedger()

    frame = build(chain, rows, ledger)

    as_of = [request["as_of"] for request in chain.contract_requests if request.get("expiration_date") is None]
    assert as_of == ["2024-10-01", "2024-10-02", "2024-11-01"], "the month's first lookup, the re-list after its failure, the next month's"
    assert [(f.stage, f.key) for f in ledger.failures] == [("option_expiries", "SPY:2024-10-01")]
    assert ledger.attempted("option_expiries") == 3
    assert list(frame.loc[frame["missing_reason"].notna(), "date"]) == ["2024-10-01"]
    np.testing.assert_allclose(
        frame["iv_30d_atm"].to_numpy(dtype=float)[1:], [chain.sigma(day) for day in WINDOW[1:]], atol=IV_ATOL, rtol=0
    )
    clean = OptionInputLedger()
    build(chain_for(rows), rows, clean)
    assert ledger.observation_digest("option_expiries") != clean.observation_digest("option_expiries"), "the lookups differ, so does the identity"


def test_an_expiry_lookup_that_hits_its_cap_is_a_typed_failure_not_a_short_list(monkeypatch):
    monkeypatch.setattr(contract_finder, "EXPIRY_LOOKUP_CONTRACTS_PER_DAY", 1)
    rows = stock_rows(WINDOW)
    ledger = OptionInputLedger()

    frame = build(chain_for(rows), rows, ledger)

    assert frame["iv_30d_atm"].isna().all() and set(frame["missing_reason"]) == {"expiry_lookup_truncated"}
    assert [(f.stage, f.key, f.error_type) for f in ledger.failures] == [
        ("option_expiries", "SPY:2024-10-01", "ExpiryLookupTruncated"),
        ("option_expiries", "SPY:2024-11-01", "ExpiryLookupTruncated"),
    ], "one lookup per month; a cut-short list is not asked again"
    assert "hit its" in ledger.failures[0].reason and "contract cap" in ledger.failures[0].reason
    assert ledger.degraded is True


def test_a_contract_lookup_that_fails_is_recorded_per_attempt_and_names_the_expiry():
    rows = stock_rows(WINDOW)
    failing = date(2024, 11, 1)
    chain = chain_for(rows, contract_failure=lambda expiry: OSError("reset") if expiry == failing.isoformat() else None)
    ledger = OptionInputLedger()

    frame = build(chain, rows, ledger)

    assert {(f.stage, f.key, f.error_type) for f in ledger.failures} == {("option_contracts", f"SPY:{failing.isoformat()}", "OSError")}
    uses = [pair for pair in expiry_pairs(frame) if failing in pair]
    assert uses, "the failing expiry was in the pair on some dates"
    assert ledger.failed_count("option_contracts") == len(uses), "a failed expiry is asked again on every date it is in the pair, and each ask is counted"
    broken = frame["iv_quality"].eq("missing").to_list()
    assert broken == [failing in pair for pair in expiry_pairs(frame)], "exactly the dates that needed it have no IV of their own"
    sound = [(day, iv) for day, iv, bad in zip(WINDOW, frame["iv_30d_atm"], broken, strict=True) if not bad]
    np.testing.assert_allclose([iv for _, iv in sound], [chain.sigma(day) for day, _ in sound], atol=IV_ATOL, rtol=0)


# ---------------------------------------------------------------------------
# R25: every option input is named, fingerprinted, and says it cannot be reproduced
# ---------------------------------------------------------------------------


def test_the_ledger_names_each_provider_input_and_fingerprints_what_it_answered():
    rows = stock_rows(WINDOW)
    chain = chain_for(rows)
    ledger = OptionInputLedger()

    frame = build(chain, rows, ledger)

    block = ledger.as_dict()
    assert set(block["inputs"]) == {"option_expiries", "option_contracts", "option_bars"}
    for stage, entry in block["inputs"].items():
        assert entry["kind"] == "provider" and entry["provider"] == "polygon" and entry["reproducible"] is False, stage
        assert entry["attempted"] > 0 and entry["failed"] == 0 and len(entry["observation_digest"]) == 64, stage
    assert block["inputs"]["option_expiries"]["attempted"] == 2, "one lookup for each of the two months"
    assert block["inputs"]["option_contracts"]["attempted"] == len({e for pair in expiry_pairs(frame) for e in pair}), (
        "one per expiry that was ever in a pair"
    )
    assert block["risk_free_rate"] == {
        "kind": "provider", "provider": "fred", "days_from_fred": 0, "days_from_default": len(WINDOW), "days_fetch_failed": 0,
        "reproducible": False,
    }
    assert block["failures"] == [] and block["failures_total"] == 0 and block["degraded"] is False


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
    monkeypatch.setattr(iv_builder, "resolve_risk_free_rate", lambda **kwargs: RateResolution(DEFAULT_RISK_FREE_RATE, RATE_SOURCE_FRED))
    ledger = OptionInputLedger()

    build(chain_for(rows), rows, ledger)

    assert (ledger.rate_days(RATE_SOURCE_FRED), ledger.rate_days(RATE_SOURCE_DEFAULT)) == (len(WINDOW), 0)
    assert ledger.degraded is False


def test_a_default_rate_with_no_fred_key_is_not_an_outage_but_one_with_a_key_is(monkeypatch):
    """#3524: no key is the same answer on every attempt; a configured key that gets nothing could answer next time."""
    rows = stock_rows(WINDOW)
    monkeypatch.setattr(fred_service, "_fetch_series", lambda *args, **kwargs: [])
    fred_service.clear_cache()
    no_key, key_but_down = OptionInputLedger(), OptionInputLedger()

    build(chain_for(rows), rows, no_key)  # the autouse fixture configures no key
    monkeypatch.setattr(settings, "FRED_API_KEY", "a-configured-key", raising=False)
    build(chain_for(rows), rows, key_but_down)

    assert no_key.rate_days(RATE_SOURCE_DEFAULT) == len(WINDOW) and no_key.rate_fetch_failed_days == 0
    assert no_key.degraded is False and no_key.as_dict()["degraded"] is False
    assert key_but_down.rate_days(RATE_SOURCE_DEFAULT) == len(WINDOW) and key_but_down.rate_fetch_failed_days == len(WINDOW)
    assert key_but_down.degraded is True
    block = key_but_down.as_dict()
    assert block["risk_free_rate"]["days_fetch_failed"] == len(WINDOW) and block["failures_total"] == 0, "no provider request failed"
    assert block["degraded"] is True
    assert combine({"SPY": no_key, "QQQ": key_but_down})["risk_free_rate"]["days_fetch_failed"] == len(WINDOW)


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
