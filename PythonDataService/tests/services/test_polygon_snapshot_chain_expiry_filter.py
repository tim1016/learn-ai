"""``list_snapshot_options_chain``: which expiries the vendor is asked for (#3221).

Run against a fake SDK that filters as the vendor does, so what the client puts
in the request is what decides which contracts come back.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.options.expiry import option_expiry_anchor_ms
from app.services.polygon_client import PolygonClientService
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import now_ms_utc
from tests._helpers.polygon_snapshot_sdk import VendorFilteringSnapshotSdk


def _contract(expiry: date, strike: float = 100.0) -> dict:
    return {
        "ticker": f"O:T{expiry:%y%m%d}C{int(strike * 1000):08d}",
        "contract_type": "call",
        "strike_price": strike,
        "expiration_ms": option_expiry_anchor_ms(expiry),
        "last_quote": {"bid": 1.0, "ask": 1.1},
    }


@pytest.fixture
def today() -> date:
    return et_date_at_ms(now_ms_utc())


def _service_over(contracts: list[dict]) -> tuple[PolygonClientService, VendorFilteringSnapshotSdk]:
    sdk = VendorFilteringSnapshotSdk(contracts, spot=100.0)
    service = PolygonClientService()
    service.client = sdk
    return service, sdk


def _expiries(chain: dict) -> list[int]:
    return sorted({c["expiration_ms"] for c in chain["contracts"]})


def test_without_a_filter_the_chain_is_todays_expiry_only(today):
    service, sdk = _service_over([_contract(today), _contract(today + timedelta(days=7))])

    chain = service.list_snapshot_options_chain(underlying_asset="SPY")

    assert _expiries(chain) == [option_expiry_anchor_ms(today)]
    assert sdk.requests == [{"expiration_date": today.isoformat()}]


def test_one_expiry_asks_for_exactly_that_date(today):
    wanted = today + timedelta(days=7)
    service, _ = _service_over([_contract(today), _contract(wanted), _contract(wanted + timedelta(days=7))])

    chain = service.list_snapshot_options_chain(underlying_asset="SPY", expiration_ms=option_expiry_anchor_ms(wanted))

    assert _expiries(chain) == [option_expiry_anchor_ms(wanted)]


def test_a_window_returns_every_expiry_inside_it_and_no_other(today):
    inside = [today + timedelta(days=d) for d in (1, 30, 65)]
    outside = [today, today + timedelta(days=66)]
    service, sdk = _service_over([_contract(d) for d in inside + outside])

    chain = service.list_snapshot_options_chain(
        underlying_asset="SPY",
        expiration_ms_gte=option_expiry_anchor_ms(today + timedelta(days=1)),
        expiration_ms_lte=option_expiry_anchor_ms(today + timedelta(days=65)),
    )

    assert _expiries(chain) == [option_expiry_anchor_ms(d) for d in inside]
    # The window replaces the single-expiry default; it does not ride along with it.
    assert "expiration_date" not in sdk.requests[0]


def test_one_expiry_and_a_window_together_are_refused(today):
    service, sdk = _service_over([_contract(today)])

    with pytest.raises(ValueError, match="not both"):
        service.list_snapshot_options_chain(
            underlying_asset="SPY",
            expiration_ms=option_expiry_anchor_ms(today),
            expiration_ms_gte=option_expiry_anchor_ms(today),
        )

    assert sdk.requests == []
