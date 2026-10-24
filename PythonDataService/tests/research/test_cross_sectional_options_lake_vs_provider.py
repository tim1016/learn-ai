"""The Cross-sectional study's option inputs: the provider path against the lake path, over one pinned payload (#3540 Part B; R14).

Golden fixture ``XSIV-001`` holds what the study's old provider path returned (the vendor's live expiries, contracts and
daily option bars, asked through the Polygon SDK) over a pinned chain: its contract-finder frame and its IV frame. The same
payload is served to the lake path at the vendor's HTTP seam (a listing as of each date, paged; one 15:59 minute per
contract-day whose close is the payload's daily price and whose volume is 500), and the study's real writers, readers,
admission, contract finder and IV builder run over what the lake stored. The provider path is gone from the tree, so the
frozen frames are the comparison; ``tests/fixtures/golden/options-pricing/XSIV-001/v1/attribution.md`` says how they were made.

Every difference is classified with the reconcile taxonomy (``.claude/rules/numerical-rigor.md``):

* on the payload itself the two paths agree on every selection column and every IV column, to the bit; the one column that
  differs is ``price_source``, ``vwap`` on the provider path and ``close_filtered`` on the lake path (a *price rule* change,
  not a number change: the payload's daily ``vw`` is the 15:59 close);
* each case below is a payload where they would not agree, run on the lake path alone and named for what the provider path
  did instead: ``data-quality`` (the input data differs), ``timestamp`` (an instant is read differently), ``decision`` (a
  different contract is picked) and ``refusal`` (a member the provider path computed on a partial chain is now left out).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import respx

from app.config import settings
from app.options_backtest.fetching import LakeOptionCapture
from app.research.options.iv_builder import build_iv_history
from app.research.options.option_inputs import (
    MemberOptionInputs,
    RateTally,
    admit_member_option_inputs,
)
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.fake_lake_catalog import FakeCatalog, install_fake_catalog, point_lake_writer_at_tmp
from tests._helpers.option_chain import OptionChain
from tests._helpers.option_chain_vendor import ChainVendor, payload_by_ticker
from tests.fixtures.golden_support.registry import default as registry
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import tmp_lake as tmp_lake

FIXTURE = "XSIV-001"
#: A Monday after the window: every session of it is over in New York and inside the options plan.
NOW_MS = et_midnight_ms(date(2024, 12, 2)) + 12 * 3_600_000
#: Volatility the chain was priced with is recovered to within the price rounding over the thinnest vega (see
#: ``tests/research/options/test_iv_builder_member_inputs.py``); the tolerance is not loosened for the lake path.
IV_ATOL = 1e-5


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
# The payload: the two paths agree to the bit
# ---------------------------------------------------------------------------


def test_the_lake_path_selects_the_same_contracts_and_derives_the_same_iv_as_the_frozen_provider_path(parity):
    iv = build_iv_history(parity.data["underlying"], parity.first, RateTally())

    assert records(parity.first.bracket) == parity.expected["bracket"], "selection: every column of the contract finder's frame"
    lake, provider = records(iv), parity.expected["iv"]
    assert len(lake) == len(provider) == len(parity.data["window"])
    assert {row["price_source"] for row in provider} == {"vwap"} and {row["price_source"] for row in lake} == {"close_filtered"}
    unchanged = lambda rows: [{k: v for k, v in row.items() if k != "price_source"} for row in rows]  # noqa: E731
    assert unchanged(lake) == unchanged(provider), "the IV columns, dte, spot, quality and gaps: bit-exact (==, no tolerance)"


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
