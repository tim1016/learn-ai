"""The lanes installation migration talks to, through the coordinator (#2268).

Every lane is reached through the coordinator's public, catalog-routed
clerk surface — ``/api/brokers/{broker}/clerks/{clerk_id}/…`` — exactly as the
operator's browser reaches it: authenticated with the data-plane control
secret, forwarded by the coordinator with that lane's coordinator service
token, fenced by the lane's routing epoch. The tool never dials a lane's
port directly and holds no lane credential.

Four catalog operations serve it (``app/broker/alpaca/clerk/fleet_adapter.py``):
``lane_stop_all_bots`` and ``lane_account_quiet_read`` for export, and
``lane_ibkr_bar_check`` and ``lane_go_live_release`` for go-live (#2269). None
drains a lane or changes an assignment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.broker.fleet.internal_http import (
    LANE_IBKR_BAR_CHECK_READ_TIMEOUT_S,
    LANE_STOP_ALL_READ_TIMEOUT_S,
)
from app.installation_migration.errors import MigrationRefused
from app.schemas.lane_go_live import (
    OLD_MACHINE_OFF_CONFIRMATION,
    LaneGoLiveReleaseReceipt,
    LaneIbkrBarCheckRead,
)
from app.schemas.lane_quiesce import LaneAccountQuietRead, LaneStopAllBotsReceipt

CONTROL_SECRET_HEADER = "X-Data-Plane-Control-Secret"
DEFAULT_COORDINATOR_URL = "http://127.0.0.1:8000"
#: Stopping every bot waits on each Stop's own bounded cancellation. Derived
#: from the coordinator's forward bound so the coordinator's answer -- even
#: its ``clerk_unreachable`` -- always lands before this client gives up.
STOP_ALL_CLIENT_TIMEOUT_S = LANE_STOP_ALL_READ_TIMEOUT_S + 10.0
#: The go-live bar check waits on one IB Gateway historical request; derived
#: the same way, so the lane's named failure always reaches the operator.
BAR_CHECK_CLIENT_TIMEOUT_S = LANE_IBKR_BAR_CHECK_READ_TIMEOUT_S + 10.0
_READ_TIMEOUT_S = 30.0


@dataclass(frozen=True, slots=True)
class Lane:
    """One clerk lane as the coordinator's directory lists it."""

    clerk_id: str
    broker: str
    lifecycle_state: str
    display_label: str


class FleetLanes(Protocol):
    """The three lane facts and acts export needs."""

    def list_lanes(self) -> list[Lane]: ...

    def stop_all_bots(
        self, lane: Lane, *, operator: str, change_ref: str
    ) -> LaneStopAllBotsReceipt: ...

    def account_quiet(self, lane: Lane) -> LaneAccountQuietRead: ...


class GoLiveLanes(Protocol):
    """The lane directory and the two lane acts go-live needs (#2269)."""

    def list_lanes(self) -> list[Lane]: ...

    def ibkr_bar_check(self, lane: Lane) -> LaneIbkrBarCheckRead: ...

    def release_go_live(
        self, lane: Lane, *, operator: str, change_ref: str
    ) -> LaneGoLiveReleaseReceipt: ...


_Answer = TypeVar("_Answer", bound=BaseModel)


def _refusal_text(body: object) -> str:
    """The reason/message a refusal body carries, in either envelope shape."""
    if isinstance(body, dict):
        inner = body.get("detail", body)
        if isinstance(inner, dict):
            reason = inner.get("reason")
            message = inner.get("message")
            if reason or message:
                return f"{reason}: {message}"
        return str(inner)
    return str(body)


class CoordinatorLanes:
    """:class:`FleetLanes` over the coordinator's HTTP surface."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_COORDINATOR_URL,
        control_secret: str | None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {CONTROL_SECRET_HEADER: control_secret} if control_secret else {}
        self._client = httpx.Client(
            base_url=base_url, headers=headers, transport=transport, timeout=_READ_TIMEOUT_S
        )

    def close(self) -> None:
        """Release the HTTP connection pool."""
        self._client.close()

    def _call(
        self, method: str, path: str, *, subject: str, timeout_s: float, json: object = None
    ) -> httpx.Response:
        try:
            return self._client.request(method, path, json=json, timeout=timeout_s)
        except httpx.HTTPError as exc:
            raise MigrationRefused(
                "coordinator_unreachable",
                f"{method} {path} ({subject}) could not reach the coordinator: {exc}",
                details={"path": path, "subject": subject},
            ) from exc

    def _json(self, response: httpx.Response, *, subject: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise MigrationRefused(
                "coordinator_answer_unreadable",
                f"{response.request.method} {response.request.url.path} ({subject}) "
                f"answered {response.status_code} without JSON.",
                details={"subject": subject, "status": response.status_code},
            ) from exc

    def _refuse(self, response: httpx.Response, *, reason: str, lane: Lane, what: str) -> None:
        body = self._json(response, subject=lane.clerk_id)
        raise MigrationRefused(
            reason,
            f"Lane {lane.clerk_id} ({lane.display_label}) refused {what}: "
            f"{response.status_code} {_refusal_text(body)}",
            details={"clerk_id": lane.clerk_id, "status": response.status_code, "body": body},
        )

    def list_lanes(self) -> list[Lane]:
        response = self._call(
            "GET", "/api/broker-clerks", subject="fleet directory", timeout_s=_READ_TIMEOUT_S
        )
        body = self._json(response, subject="fleet directory")
        if response.status_code != 200 or not isinstance(body, dict):
            raise MigrationRefused(
                "fleet_directory_unavailable",
                f"The coordinator's fleet directory answered {response.status_code}: "
                f"{_refusal_text(body)}",
                details={"status": response.status_code, "body": body},
            )
        return [
            Lane(
                clerk_id=str(row["clerk_id"]),
                broker=str(row["broker"]),
                lifecycle_state=str(row["lifecycle_state"]),
                display_label=str(row.get("display_label") or row["clerk_id"]),
            )
            for row in body.get("clerks", [])
        ]

    def _answer(
        self, response: httpx.Response, model: type[_Answer], *, lane: Lane
    ) -> _Answer:
        """A 200 body validated into the lane route's own wire model."""
        body = self._json(response, subject=lane.clerk_id)
        try:
            return model.model_validate(body)
        except ValidationError as exc:
            raise MigrationRefused(
                "coordinator_answer_unreadable",
                f"Lane {lane.clerk_id} answered {response.request.url.path} with a body "
                f"that is not a {model.__name__}: {exc}",
                details={"clerk_id": lane.clerk_id, "body": body},
            ) from exc

    def stop_all_bots(
        self, lane: Lane, *, operator: str, change_ref: str
    ) -> LaneStopAllBotsReceipt:
        path = f"/api/brokers/{lane.broker}/clerks/{lane.clerk_id}/lane/stop-all-bots"
        response = self._call(
            "POST",
            path,
            subject=lane.clerk_id,
            timeout_s=STOP_ALL_CLIENT_TIMEOUT_S,
            json={
                "command_context": {
                    "capability": "bot_action",
                    "idempotency_key": None,
                    "target": {},
                },
                "operator": operator,
                "change_ref": change_ref,
            },
        )
        if response.status_code != 200:
            self._refuse(
                response, reason="lane_stop_all_refused", lane=lane, what="to stop every bot"
            )
        return self._answer(response, LaneStopAllBotsReceipt, lane=lane)

    def account_quiet(self, lane: Lane) -> LaneAccountQuietRead:
        path = f"/api/brokers/{lane.broker}/clerks/{lane.clerk_id}/lane/account-quiet"
        response = self._call("GET", path, subject=lane.clerk_id, timeout_s=_READ_TIMEOUT_S)
        if response.status_code != 200:
            self._refuse(
                response,
                reason="lane_account_quiet_unanswered",
                lane=lane,
                what="the account-quiet read",
            )
        return self._answer(response, LaneAccountQuietRead, lane=lane)

    def ibkr_bar_check(self, lane: Lane) -> LaneIbkrBarCheckRead:
        path = f"/api/brokers/{lane.broker}/clerks/{lane.clerk_id}/lane/ibkr-bar-check"
        response = self._call(
            "GET", path, subject=lane.clerk_id, timeout_s=BAR_CHECK_CLIENT_TIMEOUT_S
        )
        if response.status_code != 200:
            self._refuse(
                response,
                reason="lane_ibkr_bar_check_failed",
                lane=lane,
                what="the IB Gateway bar check",
            )
        return self._answer(response, LaneIbkrBarCheckRead, lane=lane)

    def release_go_live(
        self, lane: Lane, *, operator: str, change_ref: str
    ) -> LaneGoLiveReleaseReceipt:
        path = f"/api/brokers/{lane.broker}/clerks/{lane.clerk_id}/lane/go-live/release"
        response = self._call(
            "POST",
            path,
            subject=lane.clerk_id,
            timeout_s=_READ_TIMEOUT_S,
            json={
                "command_context": {
                    "capability": "bot_action",
                    "idempotency_key": None,
                    "target": {},
                },
                "operator": operator,
                "change_ref": change_ref,
                "old_machine_off_confirmation": OLD_MACHINE_OFF_CONFIRMATION,
            },
        )
        if response.status_code != 200:
            self._refuse(
                response,
                reason="lane_go_live_release_refused",
                lane=lane,
                what="to release its go-live hold",
            )
        return self._answer(response, LaneGoLiveReleaseReceipt, lane=lane)


__all__ = [
    "BAR_CHECK_CLIENT_TIMEOUT_S",
    "CONTROL_SECRET_HEADER",
    "DEFAULT_COORDINATOR_URL",
    "STOP_ALL_CLIENT_TIMEOUT_S",
    "CoordinatorLanes",
    "FleetLanes",
    "GoLiveLanes",
    "Lane",
]
