"""A Clerk's ask of the coordinator for the rate and the dividends an option is priced with (#2913).

A Clerk holds no Polygon key and no FRED key (ADR 0062). The coordinator
holds both, and answers on its internal surface
(``app.routers.internal_fleet``) with what it read. This is the one call a
Clerk makes for them. It is sent as the chart-history read is: to the
coordinator's private address, with this Clerk's own identity and token, by
the fleet's pinned client.

The ask blocks. ``read_option_model_inputs`` already runs in a worker thread,
so the client is the synchronous one.

Every way the ask can fail raises ``CoordinatorDidNotAnswer`` with a code and
a clause saying what happened. The clause names no host, token or response
body. Those go to the log.
"""

from __future__ import annotations

import logging
from datetime import date

import httpx
from pydantic import TypeAdapter, ValidationError

from app.broker.fleet.internal_http import (
    FleetTransportRefused,
    build_internal_sync_client,
    enforce_private_http_target,
)
from app.broker.fleet.option_model_inputs import (
    INTERNAL_OPTION_MODEL_INPUTS_PATH,
    OPTION_MODEL_INPUTS_READ_TIMEOUT_S,
)
from app.config import fleet_settings
from app.options.expiry import option_expiry_anchor_ms
from app.schemas.fleet_option_model_inputs import (
    ModelInputsAnswer,
    ModelInputsBeingLookedUp,
    OptionModelInputsRequest,
    OptionModelInputsResponse,
    WhyNotKnown,
)

logger = logging.getLogger(__name__)

_ANSWER: TypeAdapter[ModelInputsAnswer | ModelInputsBeingLookedUp] = TypeAdapter(OptionModelInputsResponse)


class CoordinatorDidNotAnswer(Exception):
    """The coordinator gave this Clerk no answer it could use. The message is what happened, as a clause."""

    def __init__(self, why: WhyNotKnown, what_happened: str) -> None:
        super().__init__(what_happened)
        self.why = why


def ask_the_coordinator(
    underlying: str, *, at_ms: int, latest_expiry: date, wait: bool
) -> ModelInputsAnswer | ModelInputsBeingLookedUp:
    """One authenticated ask of the coordinator, and its answer to exactly that ask.

    With ``wait`` false the coordinator may answer that a lookup is running.
    Anything else that is not an answer raises ``CoordinatorDidNotAnswer``.
    """
    base_url, clerk_id, token = fleet_settings.COORDINATOR_URL, fleet_settings.CLERK_ID, fleet_settings.AGENT_SERVICE_TOKEN
    if not (base_url and clerk_id and token):
        raise CoordinatorDidNotAnswer(
            WhyNotKnown.COORDINATOR_NOT_CONFIGURED,
            "this Clerk is not set up to ask the coordinator: FLEET_COORDINATOR_URL, FLEET_AGENT_SERVICE_TOKEN or "
            "FLEET_CLERK_ID is missing",
        )
    try:
        enforce_private_http_target(base_url)
    except FleetTransportRefused as refused:
        logger.error(
            "The coordinator's address is outside the private network; the option model inputs are not asked for",
            extra={"action": "option_model_inputs_coordinator_address_refused"},
        )
        raise CoordinatorDidNotAnswer(
            WhyNotKnown.COORDINATOR_NOT_CONFIGURED,
            "the coordinator's address is not inside the private network, so nothing was sent to it",
        ) from refused
    try:
        request = OptionModelInputsRequest(
            clerk_id=clerk_id,
            underlying=underlying,
            at_ms=at_ms,
            latest_expiry_ms=option_expiry_anchor_ms(latest_expiry),
            wait=wait,
        )
    except ValidationError as not_asked:
        raise CoordinatorDidNotAnswer(
            WhyNotKnown.LOOKUP_FAILED,
            f"this is not a question the coordinator answers ({not_asked.errors()[0]['msg']})",
        ) from not_asked

    client = build_internal_sync_client(read_timeout_s=OPTION_MODEL_INPUTS_READ_TIMEOUT_S)
    try:
        response = client.post(
            f"{base_url.rstrip('/')}{INTERNAL_OPTION_MODEL_INPUTS_PATH}",
            json=request.model_dump(mode="json"),
            headers={"X-Fleet-Clerk-Id": clerk_id, "X-Fleet-Agent-Token": token},
        )
    except httpx.TimeoutException as late:
        _log_no_answer("option_model_inputs_coordinator_timed_out", error_type=type(late).__name__)
        raise CoordinatorDidNotAnswer(WhyNotKnown.COORDINATOR_TIMED_OUT, "the coordinator did not answer in time") from late
    except httpx.HTTPError as unreachable:
        _log_no_answer("option_model_inputs_coordinator_unreachable", error_type=type(unreachable).__name__)
        raise CoordinatorDidNotAnswer(
            WhyNotKnown.COORDINATOR_UNREACHABLE, "the coordinator could not be reached"
        ) from unreachable
    finally:
        client.close()

    if response.status_code != 200:
        # Reached and refused (4xx) is a defect in how this Clerk asks or is known. Failed (5xx) may pass.
        if response.status_code < 500:
            logger.error(
                "The coordinator refused an ask for the option model inputs",
                extra={"action": "option_model_inputs_coordinator_refused", "status_code": response.status_code},
            )
            raise CoordinatorDidNotAnswer(
                WhyNotKnown.COORDINATOR_REFUSED, f"the coordinator refused this Clerk's ask (HTTP {response.status_code})"
            )
        _log_no_answer("option_model_inputs_coordinator_failed", status_code=response.status_code)
        raise CoordinatorDidNotAnswer(
            WhyNotKnown.COORDINATOR_FAILED, f"the coordinator failed while answering (HTTP {response.status_code})"
        )
    try:
        answer = _ANSWER.validate_json(response.content)
    except ValidationError as unreadable:
        _log_no_answer("option_model_inputs_coordinator_answer_unreadable", error_type=type(unreadable).__name__)
        raise CoordinatorDidNotAnswer(
            WhyNotKnown.COORDINATOR_ANSWER_UNREADABLE, "the coordinator's answer could not be read"
        ) from unreadable
    if isinstance(answer, ModelInputsBeingLookedUp):
        if wait:
            _log_no_answer("option_model_inputs_coordinator_did_not_wait")
            raise CoordinatorDidNotAnswer(
                WhyNotKnown.COORDINATOR_ANSWER_UNREADABLE,
                "the coordinator answered that a lookup was running, to an ask that said it would wait",
            )
        return answer
    if (answer.underlying, answer.at_ms, answer.latest_expiry_ms) != (
        request.underlying,
        request.at_ms,
        request.latest_expiry_ms,
    ):
        _log_no_answer("option_model_inputs_coordinator_answered_another_question")
        raise CoordinatorDidNotAnswer(
            WhyNotKnown.COORDINATOR_ANSWER_UNREADABLE, "the coordinator answered another question than the one asked"
        )
    return answer


def _log_no_answer(action: str, **details: object) -> None:
    logger.warning(
        "The coordinator gave no usable answer about the option model inputs", extra={"action": action, **details}
    )


__all__ = ["CoordinatorDidNotAnswer", "ask_the_coordinator"]
