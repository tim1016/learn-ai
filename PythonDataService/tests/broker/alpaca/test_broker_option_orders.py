"""An option order through the Alpaca broker: what goes on the wire, and what its answers are taken to mean.

Every test drives the real ``AlpacaBroker``, client and alpaca-py over a
mocked HTTP wire, so the request asserted is the one Alpaca would receive.
Nothing here reaches Alpaca. The answers are its published examples
(``tests/fixtures/alpaca_documented``), not recordings.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import responses

from app.broker.alpaca.broker import AlpacaBroker, _option_order_asked_about
from app.broker.alpaca.client import _SUBMIT_VISIBILITY_GRACE_S, AlpacaTradingClient
from app.broker.alpaca.config import AlpacaSettings
from app.broker.capture.journal import CaptureJournal
from app.broker.contract.errors import (
    BrokerEvidenceUnavailable,
    BrokerOptionsNotSupported,
    BrokerOrderAnswerUnreadable,
    BrokerOrderNotPermitted,
    BrokerRateLimited,
    BrokerRequestInvalid,
    BrokerUnavailable,
    BrokerUnreachable,
)
from app.broker.contract.models import BrokerOrderLeg, OrderSide, OrderType
from app.broker.contract.options import (
    MultiLegOptionOrderRequest,
    OptionLegRequest,
    OptionPositionIntent,
    SingleOptionOrderRequest,
)
from app.options.contract import OptionContract
from tests.broker.alpaca.conftest import AlpacaFixtureLoader
from tests.broker.alpaca.documented_examples import documented

_BASE = "https://paper-api.alpaca.markets"
_ORDERS = f"{_BASE}/v2/orders"
_BY_CLIENT_ID = f"{_BASE}/v2/orders:by_client_order_id"
_FIXED_MS = 1_700_000_000_000
_REF = "learn-ai/aapl-options/v1:enter-1"
_NOT_FOUND = {"code": 40410000, "message": "order not found"}

# Alpaca's documented multi-leg answer: its parent, and its two legs.
_PARENT_ID = "83f37e9f-6b1f-49ed-8fc6-3e6af716323f"
_LEG_IDS = ["df4ff24a-c58a-4e37-8b9f-ef32b83a11f2", "ecd91110-c34d-4e9d-a7bf-a9c27c40f8b5"]


def _answer(name: str, **changes: Any) -> dict[str, Any]:
    """One of Alpaca's documented answers, echoing the client order id this test sent."""
    payload = copy.deepcopy(documented("option_orders", "documented_responses.json")[name])
    payload["client_order_id"] = _REF
    payload.update(changes)
    return payload


def _ratio_spread() -> MultiLegOptionOrderRequest:
    """One structure of three calls bought and one sold: the order Alpaca's documented multi-leg answer reads as.

    That pairing is this file's, not Alpaca's. The request Alpaca publishes
    beside that answer asks for three structures, and the answer's parent
    quantity is 1 (the fixtures' attribution has the table). These tests
    hold what is sent and how an answer is taken, not that one follows
    from the other.
    """
    return MultiLegOptionOrderRequest(
        legs=(
            OptionLegRequest(
                contract=OptionContract.from_occ_symbol("AAPL241213C00250000"),
                side=OrderSide.BUY,
                intent=OptionPositionIntent.OPEN,
                ratio=3,
            ),
            OptionLegRequest(
                contract=OptionContract.from_occ_symbol("AAPL241213C00260000"),
                side=OrderSide.SELL,
                intent=OptionPositionIntent.OPEN,
                ratio=1,
            ),
        ),
        quantity=1,
        order_type=OrderType.LIMIT,
        net_limit_price=Decimal("10"),
    )


def _long_call() -> SingleOptionOrderRequest:
    """The order Alpaca's documented single-leg answer is the answer to."""
    return SingleOptionOrderRequest(
        contract=OptionContract.from_occ_symbol("AAPL250620C00100000"),
        side=OrderSide.BUY,
        intent=OptionPositionIntent.OPEN,
        quantity=2,
        order_type=OrderType.LIMIT,
        limit_price=Decimal("10"),
    )


@pytest.fixture
def broker(tmp_path: Path) -> AlpacaBroker:
    settings = AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper")
    client = AlpacaTradingClient(
        settings=settings,
        journal=CaptureJournal(capture_dir=tmp_path / "capture", clock=lambda: _FIXED_MS),
    )
    return AlpacaBroker(client=client, settings=settings)


@pytest.fixture
def live_broker(tmp_path: Path) -> AlpacaBroker:
    settings = AlpacaSettings(
        api_key_id="k", api_secret_key="s", mode="live",
        # Every live envelope value is required configuration (ADR 0059 D4).
        live_loss_fraction=0.02, live_loss_usd=500.0, live_xh_entry_bps=10.0, live_xh_exit_bps=10.0,
    )
    client = AlpacaTradingClient(
        settings=settings,
        journal=CaptureJournal(capture_dir=tmp_path / "capture", clock=lambda: _FIXED_MS),
    )
    return AlpacaBroker(client=client, settings=settings)


def _sent(call_index: int = 0) -> dict[str, Any]:
    return json.loads(responses.calls[call_index].request.body)


def _methods_and_paths() -> list[tuple[str, str]]:
    return [(call.request.method, call.request.url.split("?")[0].removeprefix(_BASE)) for call in responses.calls]


async def _no_sleep(_seconds: float) -> None:
    return None


# ── Submitting ──────────────────────────────────────────────────────────────


def _closes_the_long_call() -> SingleOptionOrderRequest:
    return SingleOptionOrderRequest(
        contract=OptionContract.from_occ_symbol("AAPL250620C00100000"), side=OrderSide.SELL,
        intent=OptionPositionIntent.CLOSE, quantity=2, order_type=OrderType.MARKET,
    )


@responses.activate
@pytest.mark.parametrize("order", [_long_call, _closes_the_long_call, _ratio_spread], ids=["open", "close", "several legs"])
async def test_a_live_account_is_sent_no_option_order_and_the_refusal_says_nothing_was_sent(
    live_broker: AlpacaBroker, order: Any
) -> None:
    with pytest.raises(BrokerOptionsNotSupported) as refused:
        await live_broker.submit_option_order(order(), client_order_id=_REF)

    # Definitive, and before any request: nothing reached Alpaca's live endpoint.
    assert not isinstance(refused.value, BrokerUnavailable | BrokerEvidenceUnavailable)
    assert len(responses.calls) == 0


@responses.activate
async def test_a_broker_built_around_a_live_client_with_no_settings_of_its_own_sends_no_option_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process's own binding says Paper. The client that would post is bound to Live, and it is the one asked."""
    live = AlpacaSettings(
        api_key_id="k", api_secret_key="s", mode="live",
        live_loss_fraction=0.02, live_loss_usd=500.0, live_xh_entry_bps=10.0, live_xh_exit_bps=10.0,
    )
    paper_binding = AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper")
    monkeypatch.setattr("app.broker.alpaca.broker.resolved_alpaca_settings", lambda: paper_binding)
    client = AlpacaTradingClient(
        settings=live, journal=CaptureJournal(capture_dir=tmp_path / "capture", clock=lambda: _FIXED_MS)
    )
    built_around_it = AlpacaBroker(client=client)

    with pytest.raises(BrokerOptionsNotSupported):
        await built_around_it.submit_option_order(_long_call(), client_order_id=_REF)

    assert len(responses.calls) == 0


@responses.activate
async def test_a_multi_leg_order_goes_out_once_and_comes_back_as_a_parent_with_its_legs(broker: AlpacaBroker) -> None:
    responses.add(responses.POST, _ORDERS, json=_answer("MultilegOptionsOrderResponse"))

    order = await broker.submit_option_order(_ratio_spread(), client_order_id=_REF)

    assert _methods_and_paths() == [("POST", "/v2/orders")]
    assert _sent() == {
        "order_class": "mleg",
        "qty": "1",
        "type": "limit",
        "limit_price": "10",
        "time_in_force": "day",
        "client_order_id": _REF,
        "legs": [
            {"symbol": "AAPL241213C00250000", "ratio_qty": "3", "side": "buy", "position_intent": "buy_to_open"},
            {"symbol": "AAPL241213C00260000", "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
        ],
    }
    assert (order.order_id, order.client_order_id, order.status) == (_PARENT_ID, _REF, "filled")
    assert [leg.order_id for leg in order.legs] == _LEG_IDS
    assert [(leg.filled_quantity, leg.filled_avg_price, leg.status) for leg in order.legs] == [
        (3, Decimal("0.43"), "filled"),
        (1, Decimal("0.01"), "filled"),
    ]


@responses.activate
async def test_a_single_option_order_goes_out_as_a_plain_order_on_the_contract(broker: AlpacaBroker) -> None:
    responses.add(responses.POST, _ORDERS, json=_answer("OptionOrderResponse"))

    order = await broker.submit_option_order(_long_call(), client_order_id=_REF)

    assert _methods_and_paths() == [("POST", "/v2/orders")]
    assert _sent() == {
        "symbol": "AAPL250620C00100000",
        "qty": "2",
        "side": "buy",
        "position_intent": "buy_to_open",
        "type": "limit",
        "limit_price": "10",
        "time_in_force": "day",
        "client_order_id": _REF,
    }
    assert (order.multi_leg, order.legs, order.status) == (False, (), "pending_new")
    assert order.contract == _long_call().contract


@responses.activate
async def test_a_throttled_option_order_is_sent_again_under_the_same_client_order_id(
    broker: AlpacaBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 429 means the order did not land, so sending the same body again cannot make a second order."""
    monkeypatch.setattr("app.broker.alpaca.client.anyio.sleep", _no_sleep)
    responses.add(responses.POST, _ORDERS, status=429, json={"message": "too many requests"})
    responses.add(responses.POST, _ORDERS, json=_answer("MultilegOptionsOrderResponse"))

    order = await broker.submit_option_order(_ratio_spread(), client_order_id=_REF)

    assert order.order_id == _PARENT_ID
    assert len(responses.calls) == 2
    assert _sent(0) == _sent(1)
    assert _sent(1)["client_order_id"] == _REF


@pytest.mark.parametrize(
    ("status", "refusal"),
    [(403, BrokerOrderNotPermitted), (422, BrokerRequestInvalid)],
)
@responses.activate
async def test_an_option_order_alpaca_refuses_is_refused_once_and_is_not_an_unknown_outcome(
    broker: AlpacaBroker, status: int, refusal: type[Exception]
) -> None:
    responses.add(
        responses.POST, _ORDERS, status=status, json={"code": 40310000, "message": "insufficient options buying power"}
    )

    with pytest.raises(refusal) as refused:
        await broker.submit_option_order(_ratio_spread(), client_order_id=_REF)

    assert not isinstance(refused.value, BrokerUnavailable)
    assert len(responses.calls) == 1


@responses.activate
async def test_an_option_order_whose_outcome_is_unknown_stays_unknown_through_the_visibility_grace(
    broker: AlpacaBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway timeout proves nothing; for 30 seconds a lookup that finds nothing proves nothing either."""
    now = [1_000.0]
    monkeypatch.setattr("app.broker.alpaca.client.monotonic", lambda: now[0])
    responses.add(responses.POST, _ORDERS, status=504, json={"message": "gateway timeout"})
    responses.add(responses.GET, _BY_CLIENT_ID, status=404, json={"code": 40410000, "message": "order not found"})

    with pytest.raises(BrokerUnavailable):
        await broker.submit_option_order(_ratio_spread(), client_order_id=_REF)
    # One POST: the unknown outcome is never answered with a second order.
    assert _methods_and_paths() == [("POST", "/v2/orders")]

    with pytest.raises(BrokerUnavailable, match="still become visible"):
        await broker.get_option_order_by_client_order_id(_REF)

    now[0] += _SUBMIT_VISIBILITY_GRACE_S + 1
    assert await broker.get_option_order_by_client_order_id(_REF) is None


@responses.activate
async def test_an_accepted_order_whose_answer_came_without_legs_is_unknown_never_refused(broker: AlpacaBroker) -> None:
    """Alpaca took the order. Nothing after that may read as "it did not reach the broker"."""
    responses.add(responses.POST, _ORDERS, json=_answer("MultilegOptionsOrderResponse", legs=None))

    with pytest.raises(BrokerEvidenceUnavailable) as unknown:
        await broker.submit_option_order(_ratio_spread(), client_order_id=_REF)

    assert isinstance(unknown.value, BrokerUnavailable)
    # No second request is made on the submit's back: its failure could be taken for the order's.
    assert _methods_and_paths() == [("POST", "/v2/orders")]


@responses.activate
async def test_an_accepted_order_whose_answer_cannot_be_read_is_settled_by_looking_it_up(broker: AlpacaBroker) -> None:
    responses.add(responses.POST, _ORDERS, json=_answer("MultilegOptionsOrderResponse", filled_qty="some"))
    responses.add(responses.GET, _BY_CLIENT_ID, json=_answer("MultilegOptionsOrderResponse"))

    with pytest.raises(BrokerUnavailable):
        await broker.submit_option_order(_ratio_spread(), client_order_id=_REF)
    found = await broker.get_option_order_by_client_order_id(_REF)

    assert found is not None
    assert (found.order_id, found.filled_quantity) == (_PARENT_ID, 1)


# Answers Alpaca could give about an order that this port will not take as that order's.
_REFUSED_ANSWERS = [
    pytest.param({"legs": None, "id": "not-an-order-id"}, id="no legs and no usable id"),
    pytest.param({"filled_qty": "some"}, id="a quantity that will not parse"),
    pytest.param({"client_order_id": "learn-ai/another-bot/v1:enter-9"}, id="another order"),
    pytest.param({"qty": "0", "filled_qty": "0"}, id="a parent for no structures"),
    pytest.param({"symbol": "AAPL241213C00250000"}, id="a parent that names a contract of its own"),
    pytest.param({"id": "not-an-order-id"}, id="an order id that is not Alpaca's"),
]


@pytest.mark.parametrize("refused", [pytest.param({"legs": None}, id="no legs"), *_REFUSED_ANSWERS[1:]])
@responses.activate
async def test_an_order_alpaca_accepted_is_not_called_absent_because_its_answer_was_refused_here(
    broker: AlpacaBroker, monkeypatch: pytest.MonkeyPatch, refused: dict[str, Any]
) -> None:
    """Alpaca said 200, so the order exists; a lookup's 404 a moment later is its index catching up (#2913)."""
    now = [1_000.0]
    monkeypatch.setattr("app.broker.alpaca.client.monotonic", lambda: now[0])
    responses.add(responses.POST, _ORDERS, json=_answer("MultilegOptionsOrderResponse", **refused))
    responses.add(responses.GET, _BY_CLIENT_ID, status=404, json=_NOT_FOUND)

    with pytest.raises(BrokerEvidenceUnavailable):
        await broker.submit_option_order(_ratio_spread(), client_order_id=_REF)

    now[0] += 1
    with pytest.raises(BrokerUnavailable):
        await broker.get_option_order_by_client_order_id(_REF)

    # The same 30 seconds an unknown submit gets, counted from the answer that could not be used.
    now[0] += _SUBMIT_VISIBILITY_GRACE_S - 2
    with pytest.raises(BrokerUnavailable):
        await broker.get_option_order_by_client_order_id(_REF)
    now[0] += 2
    assert await broker.get_option_order_by_client_order_id(_REF) is None


@pytest.mark.parametrize("refused", _REFUSED_ANSWERS)
@responses.activate
async def test_a_lookup_answer_refused_here_does_not_end_the_grace_of_an_unknown_submit(
    broker: AlpacaBroker, monkeypatch: pytest.MonkeyPatch, refused: dict[str, Any]
) -> None:
    """Only an answer taken as this order's settles it; a 404 after any other still proves nothing (#2913)."""
    now = [1_000.0]
    monkeypatch.setattr("app.broker.alpaca.client.monotonic", lambda: now[0])
    responses.add(responses.POST, _ORDERS, status=504, json={"message": "gateway timeout"})
    responses.add(responses.GET, _BY_CLIENT_ID, json=_answer("MultilegOptionsOrderResponse", **refused))
    responses.add(responses.GET, _BY_CLIENT_ID, status=404, json=_NOT_FOUND)

    with pytest.raises(BrokerUnavailable):
        await broker.submit_option_order(_ratio_spread(), client_order_id=_REF)

    now[0] += 1
    with pytest.raises(BrokerEvidenceUnavailable):
        await broker.get_option_order_by_client_order_id(_REF)

    now[0] += 1
    with pytest.raises(BrokerUnavailable):
        await broker.get_option_order_by_client_order_id(_REF)

    # The 30 seconds count from the refused answer, the last time Alpaca answered under this client order id.
    now[0] += _SUBMIT_VISIBILITY_GRACE_S - 2
    with pytest.raises(BrokerUnavailable):
        await broker.get_option_order_by_client_order_id(_REF)
    now[0] += 2
    assert await broker.get_option_order_by_client_order_id(_REF) is None


@responses.activate
async def test_a_parent_whose_legs_could_not_be_read_again_does_not_end_the_grace_of_an_unknown_submit(
    broker: AlpacaBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second read is a request of its own; the grace must still stand while it is in flight and after it fails."""
    now = [1_000.0]
    monkeypatch.setattr("app.broker.alpaca.client.monotonic", lambda: now[0])
    responses.add(responses.POST, _ORDERS, status=504, json={"message": "gateway timeout"})
    responses.add(responses.GET, _BY_CLIENT_ID, json=_answer("MultilegOptionsOrderResponse", legs=None))
    responses.add(responses.GET, f"{_ORDERS}/{_PARENT_ID}", status=503, json={"message": "unavailable"})
    responses.add(responses.GET, _BY_CLIENT_ID, status=404, json=_NOT_FOUND)

    with pytest.raises(BrokerUnavailable):
        await broker.submit_option_order(_ratio_spread(), client_order_id=_REF)
    with pytest.raises(BrokerUnavailable):
        await broker.get_option_order_by_client_order_id(_REF)

    now[0] += 1
    with pytest.raises(BrokerUnavailable):
        await broker.get_option_order_by_client_order_id(_REF)


@responses.activate
async def test_an_answer_taken_as_this_orders_ends_the_grace_of_an_unknown_submit(
    broker: AlpacaBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1_000.0]
    monkeypatch.setattr("app.broker.alpaca.client.monotonic", lambda: now[0])
    responses.add(responses.POST, _ORDERS, status=504, json={"message": "gateway timeout"})
    responses.add(responses.GET, _BY_CLIENT_ID, json=_answer("MultilegOptionsOrderResponse"))
    responses.add(responses.GET, _BY_CLIENT_ID, status=404, json=_NOT_FOUND)

    with pytest.raises(BrokerUnavailable):
        await broker.submit_option_order(_ratio_spread(), client_order_id=_REF)
    found = await broker.get_option_order_by_client_order_id(_REF)

    assert found is not None and found.order_id == _PARENT_ID
    now[0] += 1
    assert await broker.get_option_order_by_client_order_id(_REF) is None


@responses.activate
async def test_a_stock_order_found_by_the_stock_lookup_still_ends_the_grace_as_it_always_did(
    broker: AlpacaBroker, monkeypatch: pytest.MonkeyPatch, load_alpaca_fixture: AlpacaFixtureLoader
) -> None:
    """The option lookup's way of ending the grace is its own; a stock order found is settled at once."""
    now = [1_000.0]
    monkeypatch.setattr("app.broker.alpaca.client.monotonic", lambda: now[0])
    stock_order = {**load_alpaca_fixture("orders", "orders.json")[0], "client_order_id": _REF}
    responses.add(responses.POST, _ORDERS, status=504, json={"message": "gateway timeout"})
    responses.add(responses.GET, _BY_CLIENT_ID, json=stock_order)
    responses.add(responses.GET, _BY_CLIENT_ID, status=404, json=_NOT_FOUND)

    with pytest.raises(BrokerUnavailable):
        await broker.submit(BrokerOrderLeg(symbol="SPY", side="buy", quantity=1), client_order_id=_REF)
    found = await broker.get_order_by_client_order_id(_REF)

    assert found is not None and found.client_order_id == _REF
    now[0] += 1
    assert await broker.get_order_by_client_order_id(_REF) is None


@responses.activate
async def test_an_answer_naming_another_client_order_id_is_not_this_orders_answer(broker: AlpacaBroker) -> None:
    """Another order's fills must not be counted as this one's; the outcome stays unknown."""
    someone_elses = _answer("MultilegOptionsOrderResponse", client_order_id="learn-ai/another-bot/v1:enter-9")
    responses.add(responses.POST, _ORDERS, json=someone_elses)
    responses.add(responses.GET, _BY_CLIENT_ID, json=someone_elses)

    with pytest.raises(BrokerEvidenceUnavailable) as on_submit:
        await broker.submit_option_order(_ratio_spread(), client_order_id=_REF)
    with pytest.raises(BrokerEvidenceUnavailable):
        await broker.get_option_order_by_client_order_id(_REF)

    assert isinstance(on_submit.value, BrokerUnavailable)
    assert "another-bot" in (on_submit.value.detail or "")


@pytest.mark.parametrize(
    "client_order_id",
    [
        pytest.param("", id="none"),
        pytest.param("   ", id="blank"),
        pytest.param(f" {_REF}", id="a space before it"),
        pytest.param(f"{_REF}\n", id="a line end after it"),
        pytest.param("x" * 129, id="longer than Alpaca takes"),
    ],
)
@responses.activate
async def test_an_option_order_under_a_client_order_id_alpaca_could_not_keep_is_refused_before_it_is_sent(
    broker: AlpacaBroker, client_order_id: str
) -> None:
    """Refused as a request, the way an id that is not Alpaca's is: nothing was asked, so nothing is unknown."""
    with pytest.raises(BrokerRequestInvalid) as refused:
        await broker.submit_option_order(_ratio_spread(), client_order_id=client_order_id)

    assert not isinstance(refused.value, BrokerUnavailable)
    assert len(responses.calls) == 0


@responses.activate
async def test_a_stock_leg_handed_to_the_option_submit_is_refused_before_it_is_sent(broker: AlpacaBroker) -> None:
    responses.add(responses.GET, _BY_CLIENT_ID, status=404, json=_NOT_FOUND)
    stock_leg = BrokerOrderLeg(symbol="SPY", side="buy", quantity=1)

    with pytest.raises(BrokerRequestInvalid) as refused:
        await broker.submit_option_order(stock_leg, client_order_id=_REF)  # type: ignore[arg-type]

    assert not isinstance(refused.value, BrokerUnavailable)
    # Nothing went out, so there is no grace to wait through: a lookup's "no such order" is the answer at once.
    assert await broker.get_option_order_by_client_order_id(_REF) is None
    assert _methods_and_paths() == [("GET", "/v2/orders:by_client_order_id")]


# ── An answer that cannot be used still says which order to cancel ──────────

_OTHER_ID = "30a077fa-96f6-4f20-a052-4b921ee2f243"


@responses.activate
async def test_an_accepted_order_whose_answer_cannot_be_read_still_names_the_order_to_cancel(
    broker: AlpacaBroker,
) -> None:
    """Without the id, an order Alpaca holds could be neither cancelled nor read by this port."""
    responses.add(responses.POST, _ORDERS, json=_answer("MultilegOptionsOrderResponse", filled_qty="some"))
    responses.add(responses.GET, _BY_CLIENT_ID, json=_answer("MultilegOptionsOrderResponse", filled_qty="some"))
    responses.add(responses.GET, f"{_ORDERS}/{_PARENT_ID}", json=_answer("MultilegOptionsOrderResponse", qty="x"))

    with pytest.raises(BrokerOrderAnswerUnreadable) as on_submit:
        await broker.submit_option_order(_ratio_spread(), client_order_id=_REF)
    with pytest.raises(BrokerOrderAnswerUnreadable) as on_lookup:
        await broker.get_option_order_by_client_order_id(_REF)
    with pytest.raises(BrokerOrderAnswerUnreadable) as by_its_own_id:
        await broker.get_option_order_by_broker_order_id(_PARENT_ID.upper())

    assert isinstance(on_submit.value, BrokerUnavailable)
    assert on_submit.value.broker_order_id == _PARENT_ID
    assert on_lookup.value.broker_order_id == _PARENT_ID
    assert by_its_own_id.value.broker_order_id == _PARENT_ID


@pytest.mark.parametrize("spelling", [_PARENT_ID, _PARENT_ID.upper()], ids=["as given", "upper case"])
@responses.activate
async def test_the_order_id_an_unreadable_answer_names_is_not_called_absent_a_moment_later(
    broker: AlpacaBroker, monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    """The id is handed on so the order can be read or cancelled by it. Alpaca answered for that order a
    moment ago, so its 404 to a read by the id is no more proof than its 404 to the lookup (#2913)."""
    now = [1_000.0]
    monkeypatch.setattr("app.broker.alpaca.client.monotonic", lambda: now[0])
    responses.add(responses.POST, _ORDERS, json=_answer("MultilegOptionsOrderResponse", filled_qty="some"))
    responses.add(responses.GET, f"{_ORDERS}/{_PARENT_ID}", status=404, json=_NOT_FOUND)

    with pytest.raises(BrokerOrderAnswerUnreadable) as on_submit:
        await broker.submit_option_order(_ratio_spread(), client_order_id=_REF)
    assert on_submit.value.broker_order_id == _PARENT_ID

    now[0] += 1
    with pytest.raises(BrokerUnavailable):
        await broker.get_option_order_by_broker_order_id(spelling)
    now[0] += _SUBMIT_VISIBILITY_GRACE_S - 2
    with pytest.raises(BrokerUnavailable):
        await broker.get_option_order_by_broker_order_id(spelling)
    now[0] += 2
    assert await broker.get_option_order_by_broker_order_id(spelling) is None


@responses.activate
async def test_a_parent_shown_without_its_legs_is_not_called_absent_by_a_read_of_its_own_id(
    broker: AlpacaBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No submit was made in this process. The lookup found the parent, so a 404 to its own id proves nothing."""
    now = [1_000.0]
    monkeypatch.setattr("app.broker.alpaca.client.monotonic", lambda: now[0])
    responses.add(responses.GET, _BY_CLIENT_ID, json=_answer("MultilegOptionsOrderResponse", legs=None))
    responses.add(responses.GET, f"{_ORDERS}/{_PARENT_ID}", status=404, json=_NOT_FOUND)

    with pytest.raises(BrokerOrderAnswerUnreadable) as on_lookup:
        await broker.get_option_order_by_client_order_id(_REF)
    assert on_lookup.value.broker_order_id == _PARENT_ID

    now[0] += 1
    with pytest.raises(BrokerUnavailable):
        await broker.get_option_order_by_broker_order_id(_PARENT_ID)


@responses.activate
async def test_an_order_whose_own_id_read_could_not_be_read_is_not_called_absent_a_moment_later(
    broker: AlpacaBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1_000.0]
    monkeypatch.setattr("app.broker.alpaca.client.monotonic", lambda: now[0])
    responses.add(responses.GET, f"{_ORDERS}/{_PARENT_ID}", json=_answer("MultilegOptionsOrderResponse", qty="x"))
    responses.add(responses.GET, f"{_ORDERS}/{_PARENT_ID}", status=404, json=_NOT_FOUND)

    with pytest.raises(BrokerOrderAnswerUnreadable):
        await broker.get_option_order_by_broker_order_id(_PARENT_ID)

    now[0] += 1
    with pytest.raises(BrokerUnavailable):
        await broker.get_option_order_by_broker_order_id(_PARENT_ID)


@pytest.mark.parametrize("spelling", [_OTHER_ID, _OTHER_ID.upper()], ids=["as given", "upper case"])
@pytest.mark.parametrize(
    "another_orders",
    [
        pytest.param({}, id="a whole answer about another order"),
        pytest.param({"filled_qty": "some"}, id="an unreadable answer about another order"),
    ],
)
@responses.activate
async def test_a_read_by_order_id_answered_with_another_order_is_not_called_absent_a_moment_later(
    broker: AlpacaBroker, monkeypatch: pytest.MonkeyPatch, spelling: str, another_orders: dict[str, Any]
) -> None:
    """Alpaca answered the read of this id, whatever it said. Its 404 to the same read a second later is no
    more proof than after any other answer this port would not take (#2913)."""
    now = [1_000.0]
    monkeypatch.setattr("app.broker.alpaca.client.monotonic", lambda: now[0])
    answer = _answer("MultilegOptionsOrderResponse", **another_orders)
    responses.add(responses.GET, f"{_ORDERS}/{_OTHER_ID}", json=answer)
    responses.add(responses.GET, f"{_ORDERS}/{_OTHER_ID}", status=404, json=_NOT_FOUND)

    with pytest.raises(BrokerEvidenceUnavailable) as not_taken:
        await broker.get_option_order_by_broker_order_id(spelling)
    # The answer names another order, and that order's id is still never handed on as this one's.
    assert getattr(not_taken.value, "broker_order_id", None) is None

    now[0] += 1
    with pytest.raises(BrokerUnavailable):
        await broker.get_option_order_by_broker_order_id(spelling)
    now[0] += _SUBMIT_VISIBILITY_GRACE_S - 2
    with pytest.raises(BrokerUnavailable):
        await broker.get_option_order_by_broker_order_id(spelling)
    now[0] += 2
    assert await broker.get_option_order_by_broker_order_id(spelling) is None


@responses.activate
async def test_a_parent_whose_second_read_answered_with_another_order_is_not_called_absent_by_a_read_of_its_id(
    broker: AlpacaBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lookup found the parent and read it again by its own id; Alpaca answered that read too, with
    another order. A 404 to the parent's id a second later proves nothing either."""
    now = [1_000.0]
    monkeypatch.setattr("app.broker.alpaca.client.monotonic", lambda: now[0])
    responses.add(responses.GET, _BY_CLIENT_ID, json=_answer("MultilegOptionsOrderResponse", legs=None))
    responses.add(responses.GET, f"{_ORDERS}/{_PARENT_ID}", json=_answer("MultilegOptionsOrderResponse", id=_OTHER_ID))
    responses.add(responses.GET, f"{_ORDERS}/{_PARENT_ID}", status=404, json=_NOT_FOUND)

    with pytest.raises(BrokerEvidenceUnavailable):
        await broker.get_option_order_by_client_order_id(_REF)

    now[0] += 1
    with pytest.raises(BrokerUnavailable):
        await broker.get_option_order_by_broker_order_id(_PARENT_ID)
    now[0] += _SUBMIT_VISIBILITY_GRACE_S
    assert await broker.get_option_order_by_broker_order_id(_PARENT_ID) is None


def _answer_with_first_leg(**changes: Any) -> dict[str, Any]:
    payload = _answer("MultilegOptionsOrderResponse")
    payload["legs"][0].update(changes)
    return payload


@pytest.mark.parametrize(
    ("asked", "answer"),
    [
        pytest.param(
            _PARENT_ID,
            _answer("MultilegOptionsOrderResponse", qty="0", filled_qty="0"),
            id="a multi-leg parent for no structures",
        ),
        pytest.param(_PARENT_ID, _answer_with_first_leg(symbol="MSFT241213C00250000"), id="legs on two underlyings"),
        pytest.param(
            _PARENT_ID,
            _answer("MultilegOptionsOrderResponse", side="buy"),
            id="a parent that states a side of its own",
        ),
        pytest.param(_PARENT_ID, _answer_with_first_leg(filled_avg_price="-0.43"), id="a leg filled below zero"),
        pytest.param(
            _OTHER_ID, _answer("OptionOrderResponse", limit_price="-10"), id="a single order limited below zero"
        ),
        pytest.param(_PARENT_ID, _answer_with_first_leg(id="leg-1"), id="a leg whose order id is not Alpaca's"),
    ],
)
@responses.activate
async def test_an_answer_that_contradicts_itself_is_an_unknown_outcome_that_still_names_its_order(
    broker: AlpacaBroker, asked: str, answer: dict[str, Any]
) -> None:
    """Refused by what the answer says of itself. It crosses the port as an answer that could not be read,
    with the order's id so it can be cancelled, never as the reader's own error."""
    responses.add(responses.GET, f"{_ORDERS}/{asked}", json=answer)

    with pytest.raises(BrokerOrderAnswerUnreadable) as unknown:
        await broker.get_option_order_by_broker_order_id(asked)

    assert isinstance(unknown.value, BrokerUnavailable)
    assert unknown.value.broker_order_id == asked


@responses.activate
async def test_an_accepted_order_whose_id_is_not_alpacas_is_unknown_and_hands_on_no_id(broker: AlpacaBroker) -> None:
    """Nothing could be read or cancelled by such an id, so the answer is not taken as an order held under it."""
    responses.add(responses.POST, _ORDERS, json=_answer("MultilegOptionsOrderResponse", id="not-an-order-id"))

    with pytest.raises(BrokerOrderAnswerUnreadable) as unknown:
        await broker.submit_option_order(_ratio_spread(), client_order_id=_REF)

    assert isinstance(unknown.value, BrokerUnavailable)
    assert unknown.value.broker_order_id is None


@responses.activate
async def test_an_order_read_whole_by_its_own_id_is_absent_when_alpaca_next_says_so(
    broker: AlpacaBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once an answer has been taken as the order's, the grace an unreadable answer started is over."""
    now = [1_000.0]
    monkeypatch.setattr("app.broker.alpaca.client.monotonic", lambda: now[0])
    responses.add(responses.POST, _ORDERS, json=_answer("MultilegOptionsOrderResponse", filled_qty="some"))
    responses.add(responses.GET, f"{_ORDERS}/{_PARENT_ID}", json=_answer("MultilegOptionsOrderResponse"))
    responses.add(responses.GET, f"{_ORDERS}/{_PARENT_ID}", status=404, json=_NOT_FOUND)

    with pytest.raises(BrokerOrderAnswerUnreadable):
        await broker.submit_option_order(_ratio_spread(), client_order_id=_REF)
    now[0] += 1
    read_whole = await broker.get_option_order_by_broker_order_id(_PARENT_ID)

    assert read_whole is not None and read_whole.order_id == _PARENT_ID
    assert await broker.get_option_order_by_broker_order_id(_PARENT_ID) is None


@responses.activate
async def test_an_order_a_lookup_just_showed_is_not_called_absent_because_the_submits_grace_ran_out(
    broker: AlpacaBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grace counts from the last time Alpaca answered for the order, not only from the submit. Shown at
    second 29 with an answer that cannot be read, and not found at second 31, the order is still unknown."""
    now = [1_000.0]
    monkeypatch.setattr("app.broker.alpaca.client.monotonic", lambda: now[0])
    unreadable = _answer("MultilegOptionsOrderResponse", filled_qty="some")
    responses.add(responses.POST, _ORDERS, json=unreadable)
    responses.add(responses.GET, _BY_CLIENT_ID, json=unreadable)
    responses.add(responses.GET, _BY_CLIENT_ID, status=404, json=_NOT_FOUND)

    with pytest.raises(BrokerOrderAnswerUnreadable):
        await broker.submit_option_order(_ratio_spread(), client_order_id=_REF)
    now[0] += _SUBMIT_VISIBILITY_GRACE_S - 1
    with pytest.raises(BrokerOrderAnswerUnreadable):
        await broker.get_option_order_by_client_order_id(_REF)

    now[0] += 2
    with pytest.raises(BrokerUnavailable):
        await broker.get_option_order_by_client_order_id(_REF)
    now[0] += _SUBMIT_VISIBILITY_GRACE_S
    assert await broker.get_option_order_by_client_order_id(_REF) is None


@responses.activate
async def test_a_parent_whose_legs_alpaca_would_not_show_still_names_the_order_to_cancel(broker: AlpacaBroker) -> None:
    responses.add(responses.GET, _BY_CLIENT_ID, json=_answer("MultilegOptionsOrderResponse", legs=None))
    responses.add(responses.GET, f"{_ORDERS}/{_PARENT_ID}", status=404, json=_NOT_FOUND)

    with pytest.raises(BrokerOrderAnswerUnreadable) as unknown:
        await broker.get_option_order_by_client_order_id(_REF)

    assert unknown.value.broker_order_id == _PARENT_ID


@pytest.mark.parametrize(
    "unreadable",
    [
        pytest.param({"id": "not-an-order-id"}, id="an id that is not Alpaca's"),
        pytest.param({"id": None}, id="no id"),
        pytest.param({"id": 7}, id="an id that is not text"),
        pytest.param({"client_order_id": "learn-ai/another-bot/v1:enter-9"}, id="another order's answer"),
        pytest.param({"client_order_id": None}, id="an answer that names no client order id"),
    ],
)
@responses.activate
async def test_an_unreadable_answer_never_hands_on_an_order_id_it_cannot_vouch_for(
    broker: AlpacaBroker, unreadable: dict[str, Any]
) -> None:
    """A cancel sent to the id of some other order would cancel that order."""
    answer = _answer("MultilegOptionsOrderResponse", filled_qty="some", **unreadable)
    responses.add(responses.POST, _ORDERS, json=answer)
    responses.add(responses.GET, _BY_CLIENT_ID, json=answer)

    with pytest.raises(BrokerOrderAnswerUnreadable) as on_submit:
        await broker.submit_option_order(_ratio_spread(), client_order_id=_REF)
    with pytest.raises(BrokerOrderAnswerUnreadable) as on_lookup:
        await broker.get_option_order_by_client_order_id(_REF)

    assert on_submit.value.broker_order_id is None
    assert on_lookup.value.broker_order_id is None


@responses.activate
async def test_an_unreadable_answer_about_another_broker_order_id_hands_on_no_id(broker: AlpacaBroker) -> None:
    responses.add(
        responses.GET, f"{_ORDERS}/{_OTHER_ID}", json=_answer("MultilegOptionsOrderResponse", filled_qty="some")
    )

    with pytest.raises(BrokerOrderAnswerUnreadable) as unknown:
        await broker.get_option_order_by_broker_order_id(_OTHER_ID)

    assert unknown.value.broker_order_id is None


@pytest.mark.parametrize("named", [None, ""])
@responses.activate
async def test_an_answer_that_names_no_client_order_id_is_not_this_orders_answer(
    broker: AlpacaBroker, named: str | None
) -> None:
    """Naming no order is not naming ours."""
    answer = _answer("MultilegOptionsOrderResponse", client_order_id=named)
    responses.add(responses.POST, _ORDERS, json=answer)
    responses.add(responses.GET, _BY_CLIENT_ID, json=answer)

    with pytest.raises(BrokerEvidenceUnavailable) as on_submit:
        await broker.submit_option_order(_ratio_spread(), client_order_id=_REF)
    with pytest.raises(BrokerEvidenceUnavailable):
        await broker.get_option_order_by_client_order_id(_REF)

    # A whole answer about another order is no handle on this one.
    assert not isinstance(on_submit.value, BrokerOrderAnswerUnreadable)


# ── Looking an order up ─────────────────────────────────────────────────────


@responses.activate
async def test_a_lookup_by_client_order_id_returns_the_parent_with_its_legs(broker: AlpacaBroker) -> None:
    responses.add(responses.GET, _BY_CLIENT_ID, json=_answer("MultilegOptionsOrderResponse"))

    order = await broker.get_option_order_by_client_order_id(_REF)

    assert order is not None
    assert responses.calls[0].request.params == {"client_order_id": _REF}
    assert len(responses.calls) == 1
    assert (order.order_id, order.multi_leg) == (_PARENT_ID, True)
    assert [leg.order_id for leg in order.legs] == _LEG_IDS


@responses.activate
async def test_a_parent_that_comes_back_without_legs_is_read_again_by_its_own_id_with_them(
    broker: AlpacaBroker,
) -> None:
    """Alpaca documents the way to ask for nested legs on the read by broker order id only."""
    responses.add(responses.GET, _BY_CLIENT_ID, json=_answer("MultilegOptionsOrderResponse", legs=None))
    responses.add(responses.GET, f"{_ORDERS}/{_PARENT_ID}", json=_answer("MultilegOptionsOrderResponse"))

    order = await broker.get_option_order_by_client_order_id(_REF)

    assert order is not None
    assert _methods_and_paths() == [
        ("GET", "/v2/orders:by_client_order_id"),
        ("GET", f"/v2/orders/{_PARENT_ID}"),
    ]
    assert responses.calls[1].request.params["nested"].lower() == "true"
    assert [leg.order_id for leg in order.legs] == _LEG_IDS


@responses.activate
async def test_a_parent_found_by_client_id_and_not_by_its_own_id_leaves_the_outcome_unknown(
    broker: AlpacaBroker,
) -> None:
    responses.add(responses.GET, _BY_CLIENT_ID, json=_answer("MultilegOptionsOrderResponse", legs=None))
    responses.add(responses.GET, f"{_ORDERS}/{_PARENT_ID}", status=404, json={"message": "order not found"})

    with pytest.raises(BrokerEvidenceUnavailable) as unknown:
        await broker.get_option_order_by_client_order_id(_REF)

    assert isinstance(unknown.value, BrokerUnavailable)


@pytest.mark.parametrize("order_id", ["not-an-order-id", None, "", 7])
@responses.activate
async def test_a_parent_without_legs_and_without_a_usable_id_leaves_the_outcome_unknown(
    broker: AlpacaBroker, order_id: object
) -> None:
    responses.add(responses.GET, _BY_CLIENT_ID, json=_answer("MultilegOptionsOrderResponse", legs=None, id=order_id))

    with pytest.raises(BrokerEvidenceUnavailable):
        await broker.get_option_order_by_client_order_id(_REF)

    assert len(responses.calls) == 1


def test_an_answer_is_never_taken_without_an_id_to_hold_it_to() -> None:
    """The check that an answer is about the order asked for cannot be skipped by asking with no id."""
    with pytest.raises(ValueError, match="none was given"):
        _option_order_asked_about(_answer("MultilegOptionsOrderResponse"))


@responses.activate
async def test_a_second_read_that_answers_about_another_order_is_not_this_orders_answer(broker: AlpacaBroker) -> None:
    """The parent was read again by its own id; an answer under another id is some other order's legs."""
    responses.add(responses.GET, _BY_CLIENT_ID, json=_answer("MultilegOptionsOrderResponse", legs=None))
    responses.add(responses.GET, f"{_ORDERS}/{_PARENT_ID}", json=_answer("MultilegOptionsOrderResponse", id=_OTHER_ID))

    with pytest.raises(BrokerEvidenceUnavailable):
        await broker.get_option_order_by_client_order_id(_REF)


@pytest.mark.parametrize("status", [429, 500, 503])
@responses.activate
async def test_an_option_lookup_alpaca_could_not_answer_is_never_an_absent_order(
    broker: AlpacaBroker, status: int
) -> None:
    """A throttle or an outage is no answer; a cancel is called confirmed on the strength of these reads."""
    responses.add(responses.GET, f"{_ORDERS}/{_PARENT_ID}", status=status, json={"message": "no answer"})
    responses.add(responses.GET, _BY_CLIENT_ID, status=status, json={"message": "no answer"})
    responses.add(responses.GET, _BY_CLIENT_ID, json=_answer("MultilegOptionsOrderResponse", legs=None))

    with pytest.raises((BrokerUnavailable, BrokerRateLimited)):
        await broker.get_option_order_by_broker_order_id(_PARENT_ID)
    with pytest.raises((BrokerUnavailable, BrokerRateLimited)):
        await broker.get_option_order_by_client_order_id(_REF)
    # Found by its client order id without legs, and the second read is the one that gets no answer.
    with pytest.raises((BrokerUnavailable, BrokerRateLimited)):
        await broker.get_option_order_by_client_order_id(_REF)

    assert len(responses.calls) == 4


@responses.activate
async def test_an_order_alpaca_has_never_seen_is_absent(broker: AlpacaBroker) -> None:
    responses.add(responses.GET, _BY_CLIENT_ID, status=404, json={"code": 40410000, "message": "order not found"})

    assert await broker.get_option_order_by_client_order_id(_REF) is None


@responses.activate
async def test_a_lookup_alpaca_cannot_answer_leaves_the_outcome_unknown(broker: AlpacaBroker) -> None:
    responses.add(responses.GET, _BY_CLIENT_ID, status=503, json={"message": "unavailable"})

    with pytest.raises(BrokerUnreachable):
        await broker.get_option_order_by_client_order_id(_REF)


@responses.activate
async def test_a_stock_order_found_by_an_option_lookup_is_not_read_as_an_option_order(
    broker: AlpacaBroker, load_alpaca_fixture: AlpacaFixtureLoader
) -> None:
    stock_order = load_alpaca_fixture("orders", "orders.json")[0]
    responses.add(responses.GET, _BY_CLIENT_ID, json=stock_order)

    with pytest.raises(BrokerEvidenceUnavailable):
        await broker.get_option_order_by_client_order_id(_REF)


@responses.activate
async def test_a_lookup_by_broker_order_id_asks_for_the_legs_nested(broker: AlpacaBroker) -> None:
    responses.add(responses.GET, f"{_ORDERS}/{_PARENT_ID}", json=_answer("MultilegOptionsOrderResponse"))

    order = await broker.get_option_order_by_broker_order_id(_PARENT_ID)

    assert order is not None
    assert responses.calls[0].request.params["nested"].lower() == "true"
    assert [(leg.order_id, leg.filled_quantity) for leg in order.legs] == list(zip(_LEG_IDS, [3, 1], strict=True))


@responses.activate
async def test_a_lookup_by_broker_order_id_reads_a_single_option_order(broker: AlpacaBroker) -> None:
    single_id = "30a077fa-96f6-4f20-a052-4b921ee2f243"
    responses.add(responses.GET, f"{_ORDERS}/{single_id}", json=_answer("OptionOrderResponse"))

    order = await broker.get_option_order_by_broker_order_id(single_id)

    assert order is not None
    assert (order.order_id, order.multi_leg, order.quantity, order.filled_quantity) == (single_id, False, 2, 0)


@responses.activate
async def test_an_answer_about_another_broker_order_id_is_not_this_orders_answer(broker: AlpacaBroker) -> None:
    asked = "30a077fa-96f6-4f20-a052-4b921ee2f243"
    responses.add(responses.GET, f"{_ORDERS}/{asked}", json=_answer("MultilegOptionsOrderResponse"))

    with pytest.raises(BrokerEvidenceUnavailable):
        await broker.get_option_order_by_broker_order_id(asked)


@pytest.mark.parametrize(
    "spelling",
    [
        pytest.param(_PARENT_ID.upper(), id="upper case"),
        pytest.param(_PARENT_ID.replace("-", ""), id="no hyphens"),
        pytest.param("{" + _PARENT_ID + "}", id="in braces"),
        pytest.param(f"urn:uuid:{_PARENT_ID}", id="as a urn"),
    ],
)
@responses.activate
async def test_a_broker_order_id_is_the_same_id_however_it_is_spelled(broker: AlpacaBroker, spelling: str) -> None:
    """Alpaca is asked for the right order under each spelling, so its answer must not be refused as another's."""
    responses.add(responses.GET, f"{_ORDERS}/{_PARENT_ID}", json=_answer("MultilegOptionsOrderResponse"))

    order = await broker.get_option_order_by_broker_order_id(spelling)

    assert order is not None and order.order_id == _PARENT_ID


@responses.activate
async def test_a_broker_order_id_alpaca_does_not_know_is_absent(broker: AlpacaBroker) -> None:
    responses.add(responses.GET, f"{_ORDERS}/{_PARENT_ID}", status=404, json={"message": "order not found"})

    assert await broker.get_option_order_by_broker_order_id(_PARENT_ID) is None


@responses.activate
async def test_an_id_that_is_not_alpacas_is_refused_before_anything_is_asked(broker: AlpacaBroker) -> None:
    with pytest.raises(BrokerRequestInvalid, match="not an Alpaca order id"):
        await broker.get_option_order_by_broker_order_id(_REF)

    assert len(responses.calls) == 0


# ── The contract reference ──────────────────────────────────────────────────


@responses.activate
async def test_a_contract_is_looked_up_in_the_trading_api_by_its_symbol(broker: AlpacaBroker) -> None:
    contract = OptionContract.from_occ_symbol("AAPL250620C00100000")
    responses.add(
        responses.GET,
        f"{_BASE}/v2/options/contracts/AAPL250620C00100000",
        json=documented("option_contracts", "documented_contracts.json")["reference_example"],
    )

    listing = await broker.get_option_contract(contract)

    assert listing is not None
    assert _methods_and_paths() == [("GET", "/v2/options/contracts/AAPL250620C00100000")]
    assert (listing.symbol, listing.tradable, listing.style) == ("AAPL250620C00100000", True, "american")


@responses.activate
async def test_alpacas_not_found_answer_is_a_contract_it_does_not_list(broker: AlpacaBroker) -> None:
    unlisted = OptionContract.from_occ_symbol("TSLA231110C00021000")
    responses.add(
        responses.GET,
        f"{_BASE}/v2/options/contracts/TSLA231110C00021000",
        status=404,
        json=documented("option_contracts", "documented_contracts.json")["not_found"],
    )

    assert await broker.get_option_contract(unlisted) is None


@responses.activate
async def test_a_contract_lookup_alpaca_cannot_answer_is_an_outage_not_an_unlisted_contract(
    broker: AlpacaBroker,
) -> None:
    contract = OptionContract.from_occ_symbol("AAPL250620C00100000")
    responses.add(
        responses.GET, f"{_BASE}/v2/options/contracts/AAPL250620C00100000", status=500, json={"message": "error"}
    )

    with pytest.raises(BrokerUnreachable):
        await broker.get_option_contract(contract)
