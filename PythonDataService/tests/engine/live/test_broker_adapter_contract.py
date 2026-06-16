"""IBrokerAdapter protocol contract tests (PRD-C).

Both the executing IbkrBrokerAdapter and the shadow NoSubmitBrokerAdapter
implement the same typed ``BrokerAdapter`` contract LiveEngine consumes, so
the engine never depends on a concrete adapter. The shadow adapter, by
construction, can never reach the broker's order-submission path.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.engine.live.live_portfolio import BrokerAdapter, IbkrBrokerAdapter
from app.engine.live.no_submit_broker_adapter import NoSubmitBrokerAdapter


def _ibkr_adapter() -> IbkrBrokerAdapter:
    client = SimpleNamespace(
        ib=SimpleNamespace(),
        connected_account="DU1",
        is_connected=lambda: True,
        require_connected=lambda: None,
        require_live=lambda: None,
    )
    return IbkrBrokerAdapter(client)


def _no_submit_adapter() -> NoSubmitBrokerAdapter:
    return NoSubmitBrokerAdapter(
        SimpleNamespace(),
        strategy_instance_id="inst",
        bot_order_namespace="learn-ai/inst/abc",
    )


def test_both_adapters_conform_to_broker_adapter_protocol() -> None:
    assert isinstance(_ibkr_adapter(), BrokerAdapter)
    assert isinstance(_no_submit_adapter(), BrokerAdapter)


def test_both_adapters_expose_the_protocol_methods() -> None:
    for adapter in (_ibkr_adapter(), _no_submit_adapter()):
        for method in ("fetch_account_summary", "fetch_positions", "place_order", "cancel_open_orders"):
            assert callable(getattr(adapter, method)), f"{type(adapter).__name__}.{method}"
