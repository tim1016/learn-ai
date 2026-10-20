"""The migration reaches lanes only through the coordinator's routed surface (#2268).

Pinned against an ``httpx.MockTransport``: the exact clerk-scoped paths, the
control-secret header, the command envelope on the stop, and that
every non-200 answer is a refusal naming the lane.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from app.installation_migration.errors import MigrationRefused
from app.installation_migration.lanes import CoordinatorLanes, Lane

_LANE = Lane(
    clerk_id="clrk_live", broker="alpaca", lifecycle_state="provisioned", display_label="Live"
)


def _lanes(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[CoordinatorLanes, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return (
        CoordinatorLanes(
            base_url="http://coordinator.test",
            control_secret="s3cret",
            transport=httpx.MockTransport(record),
        ),
        seen,
    )


def test_list_lanes_reads_the_directory_with_the_control_secret() -> None:
    lanes, seen = _lanes(
        lambda _request: httpx.Response(
            200,
            json={
                "observed_at_ms": 1,
                "clerks": [
                    {
                        "clerk_id": "clrk_live",
                        "broker": "alpaca",
                        "lifecycle_state": "provisioned",
                        "display_label": "Live",
                    }
                ],
            },
        )
    )

    assert lanes.list_lanes() == [_LANE]
    assert seen[0].url.path == "/api/broker-clerks"
    assert seen[0].headers["X-Data-Plane-Control-Secret"] == "s3cret"


_RECEIPT = {
    "receipt_id": "rcpt-1",
    "requested_at_ms": 1,
    "completed_at_ms": 2,
    "operator": "inkant",
    "change_ref": "migrate",
    "reason": "lane_stop_all",
    "stopped": [{"strategy_instance_id": "ema-1", "run_id": "run-1"}],
    "intent_stopped": [],
    "refused": [],
    "still_running": False,
    "all_stopped": True,
}
_QUIET = {
    "account_id": "PA-1",
    "observed_at_ms": 5,
    "runner_idle": True,
    "broker_work_ended": True,
    "account_flat": True,
    "intents_resolved": True,
    "quiet": True,
    "outstanding": [],
}


def test_stop_all_bots_posts_the_command_envelope_to_the_clerk_scoped_route() -> None:
    lanes, seen = _lanes(lambda _request: httpx.Response(200, json=_RECEIPT))

    receipt = lanes.stop_all_bots(_LANE, operator="inkant", change_ref="migrate")

    assert receipt.receipt_id == "rcpt-1"
    assert [bot.strategy_instance_id for bot in receipt.stopped] == ["ema-1"]
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/api/brokers/alpaca/clerks/clrk_live/lane/stop-all-bots"
    body = json.loads(request.content)
    assert body["command_context"]["capability"] == "bot_action"
    assert body["operator"] == "inkant"
    assert body["change_ref"] == "migrate"


def test_account_quiet_reads_the_clerk_scoped_route() -> None:
    lanes, seen = _lanes(lambda _request: httpx.Response(200, json=_QUIET))

    answer = lanes.account_quiet(_LANE)

    assert (answer.account_id, answer.account_flat) == ("PA-1", True)
    assert seen[0].url.path == "/api/brokers/alpaca/clerks/clrk_live/lane/account-quiet"


def test_a_200_body_that_is_not_the_route_s_model_is_a_refusal_naming_the_lane() -> None:
    lanes, _seen = _lanes(lambda _request: httpx.Response(200, json={"quiet": True}))

    with pytest.raises(MigrationRefused) as refused:
        lanes.account_quiet(_LANE)

    assert refused.value.reason == "coordinator_answer_unreadable"
    assert refused.value.details["clerk_id"] == "clrk_live"


def test_an_unreachable_coordinator_refuses() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    lanes, _seen = _lanes(fail)

    with pytest.raises(MigrationRefused) as refused:
        lanes.list_lanes()

    assert refused.value.reason == "coordinator_unreachable"


_BAR_CHECK = {
    "symbol": "SPY",
    "bar_count": 1950,
    "first_bar_start_ms": 1_788_800_000_000,
    "last_bar_end_ms": 1_789_000_000_000,
    "checked_at_ms": 1_789_100_000_000,
}


def test_ibkr_bar_check_reads_the_clerk_scoped_route() -> None:
    lanes, seen = _lanes(lambda _request: httpx.Response(200, json=_BAR_CHECK))

    check = lanes.ibkr_bar_check(_LANE)

    assert check.bar_count == 1950
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/brokers/alpaca/clerks/clrk_live/lane/ibkr-bar-check"


def test_release_posts_the_envelope_and_the_confirmation_words() -> None:
    receipt = {
        "receipt_id": "rel-1",
        "released_at_ms": 1_789_100_000_001,
        "operator": "inkant",
        "change_ref": "go-live",
        "was_held": True,
        "marker": None,
        "marker_problem": None,
        "bar_check": _BAR_CHECK,
    }
    lanes, seen = _lanes(lambda _request: httpx.Response(200, json=receipt))

    answer = lanes.release_go_live(_LANE, operator="inkant", change_ref="go-live")

    assert answer.was_held is True
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/api/brokers/alpaca/clerks/clrk_live/lane/go-live/release"
    body = json.loads(request.content)
    assert body["command_context"]["capability"] == "bot_action"
    assert body["old_machine_off_confirmation"] == "the old machine is off"
    assert (body["operator"], body["change_ref"]) == ("inkant", "go-live")


@pytest.mark.parametrize(
    ("status", "detail_reason", "detail_message", "operation", "reason"),
    [
        pytest.param(
            409,
            "lane_stop_all_incomplete",
            "ema-1 did not stop",
            lambda lanes: lanes.stop_all_bots(_LANE, operator="inkant", change_ref="migrate"),
            "lane_stop_all_refused",
            id="an_incomplete_stop",
        ),
        pytest.param(
            503,
            "lane_account_quiet_unobservable",
            "no broker",
            lambda lanes: lanes.account_quiet(_LANE),
            "lane_account_quiet_unanswered",
            id="an_unobservable_account",
        ),
        pytest.param(
            503,
            "ibkr_no_bars",
            "no bars came back",
            lambda lanes: lanes.ibkr_bar_check(_LANE),
            "lane_ibkr_bar_check_failed",
            id="a_lane_whose_gateway_returns_no_bars",
        ),
        pytest.param(
            409,
            "lane_go_live_bar_check_required",
            "no fresh bar check",
            lambda lanes: lanes.release_go_live(_LANE, operator="inkant", change_ref="go-live"),
            "lane_go_live_release_refused",
            id="a_refused_release",
        ),
    ],
)
def test_a_non_200_answer_is_a_refusal_naming_the_lane_and_why(
    status: int,
    detail_reason: str,
    detail_message: str,
    operation: Callable[[CoordinatorLanes], object],
    reason: str,
) -> None:
    lanes, _seen = _lanes(
        lambda _request: httpx.Response(
            status, json={"detail": {"reason": detail_reason, "message": detail_message}}
        )
    )

    with pytest.raises(MigrationRefused) as refused:
        operation(lanes)

    assert refused.value.reason == reason, "the refusal carries the operation's own reason"
    assert refused.value.details["clerk_id"] == "clrk_live", "the refusal names the lane in its details"
    assert "clrk_live" in refused.value.message, "the refusal names the lane in its message"
    assert detail_reason in refused.value.message, "the refusal says why the coordinator refused"
