"""An ENTER whose order POST is answered 2xx with JSON that is not an order (#3216).

Driven through the real chain a live ENTER takes: the Clerk's ``submit_enter``
over ``ClaimedBrokerIO``, ``AlpacaBroker.submit``, the real
``AlpacaTradingClient`` and a faked alpaca-py ``post``, which in raw mode hands
back whatever JSON Alpaca sent.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.broker import AlpacaBroker
from app.broker.alpaca.clerk.sqlite.enter import resolve_enter_submission
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.client import AlpacaTradingClient
from app.broker.contract.models import BrokerOrderLeg
from tests.broker.alpaca.clerk.sqlite.budgeted_run_fixture import submit_budgeted_test_enter, submit_budgeted_test_run
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _clock_at
from tests.broker.alpaca.conftest import ApiErrorFactory

ACCOUNT_ID = "PA-TEST"
SID = "spy-bot"
RUN_ID = "run-1"
_ALPACA_ORDER_ID = "00000000-0000-0000-0000-0000000000a1"


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[ClerkSqliteRepository]:
    clock = _clock_at(NOON)
    r = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    r.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    submit_budgeted_test_run(r, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    yield r
    r.close()


class _AlpacaThatPlacesTheOrderButAnswersBadly:
    """alpaca-py's raw client: the POST places the order and answers ``post_answer``."""

    def __init__(self, post_answer: object, make_api_error: ApiErrorFactory) -> None:
        self.post_answer = post_answer
        self.post_calls: list[dict[str, Any]] = []
        self.lookup_calls: list[str] = []
        self.lookup_index_has_caught_up = True
        self._placed: dict[str, dict[str, Any]] = {}
        self._make_api_error = make_api_error

    def post(self, path: str, data: dict[str, Any] | None = None) -> object:
        assert path == "/orders" and data is not None
        self.post_calls.append(data)
        self._placed[data["client_order_id"]] = {
            "id": _ALPACA_ORDER_ID,
            "client_order_id": data["client_order_id"],
            "symbol": data["symbol"],
            "side": data["side"],
            "type": data["type"],
            "order_type": data["type"],
            "time_in_force": data["time_in_force"],
            "qty": data["qty"],
            "filled_qty": "0",
            "status": "accepted",
            "created_at": "2023-11-14T22:13:21Z",
            "updated_at": "2023-11-14T22:13:21Z",
            "submitted_at": "2023-11-14T22:13:21Z",
        }
        return self.post_answer

    def get_order_by_client_id(self, client_id: str) -> dict[str, Any]:
        self.lookup_calls.append(client_id)
        if not self.lookup_index_has_caught_up or client_id not in self._placed:
            raise self._make_api_error(404, message="order not found")
        return self._placed[client_id]


def _leg() -> BrokerOrderLeg:
    return BrokerOrderLeg(symbol="SPY", side="buy", quantity=1)


_UNUSABLE_ANSWERS = [
    pytest.param([], id="empty-list"),
    pytest.param([{"id": _ALPACA_ORDER_ID}], id="list-of-an-order"),
    pytest.param("accepted", id="string"),
    pytest.param(7, id="number"),
    pytest.param(True, id="boolean"),
    # An object, but not the order that was sent: the Clerk folds it unknown and looks the order up.
    pytest.param({}, id="empty-object"),
    pytest.param({"id": _ALPACA_ORDER_ID, "client_order_id": "another-bot:enter-9"}, id="another-order"),
]


@pytest.mark.parametrize("answer", _UNUSABLE_ANSWERS)
async def test_an_unusable_answer_to_the_order_post_resolves_by_lookup_and_sends_no_second_order(
    repo: ClerkSqliteRepository, make_api_error: ApiErrorFactory, answer: object
) -> None:
    """The order was placed. The answer is an unknown outcome, never a raw ``TypeError``.

    The ENTER folds ``unknown`` and resolves by the exact lookup of its
    client order id; the order is sent once.
    """
    sdk = _AlpacaThatPlacesTheOrderButAnswersBadly(answer, make_api_error)
    broker = AlpacaBroker(client=AlpacaTradingClient(client_factory=lambda: sdk))

    submission = await submit_budgeted_test_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        trade=broker,
    )

    assert len(sdk.post_calls) == 1
    assert sdk.lookup_calls == [submission.order_ref]
    effect = repo.effect_operation(submission.effect_operation_id)
    assert effect is not None and effect.state == "in_progress"
    order = repo.order(submission.order_ref)
    assert order is not None and order.broker_order_id == _ALPACA_ORDER_ID


@pytest.mark.parametrize("answer", [[], {}], ids=["non-object", "empty-object"])
async def test_an_unusable_answer_whose_order_is_not_yet_visible_stays_unknown_until_the_sweep_finds_it(
    repo: ClerkSqliteRepository, make_api_error: ApiErrorFactory, answer: object
) -> None:
    """Alpaca's lookup index can trail its own 200: that 404 is not "never placed".

    The visibility window the client keeps for an unknown submit stays open
    over a non-object or empty answer, so the lookup's 404 is unknown too and
    the ENTER is not folded failed. The sweep later finds the order; still
    one order sent.
    """
    sdk = _AlpacaThatPlacesTheOrderButAnswersBadly(answer, make_api_error)
    sdk.lookup_index_has_caught_up = False
    broker = AlpacaBroker(client=AlpacaTradingClient(client_factory=lambda: sdk))

    submission = await submit_budgeted_test_enter(
        repo,
        account_id=ACCOUNT_ID,
        strategy_instance_id=SID,
        decision_id="dec-1",
        lifecycle_run_id=RUN_ID,
        leg=_leg(),
        trade=broker,
    )

    effect = repo.effect_operation(submission.effect_operation_id)
    assert effect is not None and effect.state == "unknown"
    assert len(sdk.post_calls) == 1

    sdk.lookup_index_has_caught_up = True
    resolved = await resolve_enter_submission(repo, order_ref=submission.order_ref, trade=broker)

    effect = repo.effect_operation(resolved.effect_operation_id)
    assert effect is not None and effect.state == "in_progress"
    order = repo.order(submission.order_ref)
    assert order is not None and order.broker_order_id == _ALPACA_ORDER_ID
    assert len(sdk.post_calls) == 1
