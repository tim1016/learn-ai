"""The Alpaca capability descriptor must not advertise unbuildable order types.

Callers gate on ``capabilities()`` (design decision D2), so an advertised order
type that ``BrokerOrderLeg`` cannot construct is a false ``yes`` — a caller
asking "can I place a stop order?" would be told it can, then hit a
``ValidationError`` at construction time. These tests pin the advertised set to
the constructible ``OrderType`` so the two cannot silently drift apart.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.broker.alpaca.broker import (
    ALPACA_EXTENDED_HOURS_WINDOW,
    ALPACA_LIVE_CAPABILITIES,
    ALPACA_PAPER_CAPABILITIES,
    AlpacaBroker,
)
from app.broker.alpaca.clerk.synthetic_broker import SYNTHETIC_CAPABILITIES
from app.broker.alpaca.config import AlpacaSettings
from app.broker.contract.models import BrokerOrderLeg, OrderType

_DESCRIPTORS = [ALPACA_PAPER_CAPABILITIES, ALPACA_LIVE_CAPABILITIES]


@pytest.mark.parametrize("capabilities", _DESCRIPTORS)
def test_advertised_order_types_match_the_constructible_enum(capabilities) -> None:
    buildable = {member.value for member in OrderType}

    assert set(capabilities.supported_order_types) == buildable


@pytest.mark.parametrize("capabilities", _DESCRIPTORS)
def test_every_advertised_order_type_builds_a_valid_leg(capabilities) -> None:
    for order_type in capabilities.supported_order_types:
        kwargs: dict[str, object] = {
            "symbol": "SPY",
            "side": "buy",
            "quantity": 1,
            "order_type": order_type,
        }
        if order_type == "limit":
            kwargs["limit_price"] = 100.0

        BrokerOrderLeg(**kwargs)


def test_live_descriptor_differs_from_paper_only_in_paper_only() -> None:
    assert ALPACA_PAPER_CAPABILITIES.paper_only is True
    assert ALPACA_LIVE_CAPABILITIES.paper_only is False
    assert ALPACA_LIVE_CAPABILITIES.model_dump(exclude={"paper_only"}) == (
        ALPACA_PAPER_CAPABILITIES.model_dump(exclude={"paper_only"})
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("paper", ALPACA_PAPER_CAPABILITIES), ("live", ALPACA_LIVE_CAPABILITIES)],
)
def test_capabilities_select_by_settings_mode(
    monkeypatch: pytest.MonkeyPatch, mode: str, expected
) -> None:
    live_values = {
        "live_loss_fraction": 0.02, "live_loss_usd": 500.0, "live_shadow_sessions": 5,
        "live_arming_max_sessions": 20, "live_xh_entry_bps": 10.0, "live_xh_exit_bps": 10.0,
    }
    settings = AlpacaSettings(api_key_id="k", api_secret_key="s", mode=mode, **(live_values if mode == "live" else {}))
    monkeypatch.setattr("app.broker.alpaca.broker.resolved_alpaca_settings", lambda: settings)

    assert AlpacaBroker(client=MagicMock()).capabilities() is expected


@pytest.mark.parametrize("capabilities", [*_DESCRIPTORS, SYNTHETIC_CAPABILITIES])
def test_every_descriptor_declares_the_alpaca_extended_window(capabilities) -> None:
    assert capabilities.supports_extended_hours is True
    assert capabilities.extended_hours_window == ALPACA_EXTENDED_HOURS_WINDOW


def test_the_declared_window_is_alpacas_documented_session() -> None:
    # 04:00–20:00 ET, Alpaca "Orders at Alpaca" § Extended Hours Trading (see docs/references/alpaca-extended-hours.md).
    assert ALPACA_EXTENDED_HOURS_WINDOW.open_minute_et == 4 * 60
    assert ALPACA_EXTENDED_HOURS_WINDOW.close_minute_et == 20 * 60
