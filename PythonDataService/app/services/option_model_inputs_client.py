"""A Clerk's ask of the coordinator for the rate and the dividends an option is priced with (#2913).

A Clerk holds no Polygon key and no FRED key (ADR 0076). The coordinator
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

``AskedOfTheCoordinator`` is that ask as the reading takes it: one ask for
both facts, each turned into what the reading keeps. The wire's own models
stop here. The reading itself (``app.services.option_model_inputs``) knows
only the facts, so a process that reads the vendors never loads them.
"""

from __future__ import annotations

import logging
import threading
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
from app.options.model_input_reasons import WhyNotKnown
from app.options.resolver import DividendOn
from app.schemas.fleet_option_model_inputs import (
    DividendsNotKnown,
    ModelInputsAnswer,
    ModelInputsBeingLookedUp,
    OptionModelInputsRequest,
    OptionModelInputsResponse,
)
from app.services.option_model_inputs import DividendsLearned, ModelInputsBeingRead, RateLearned, the_default

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
    except ValueError as unreadable:
        # Not an address at all, such as one with half a bracket.
        raise _not_set_up_to_ask(unreadable) from unreadable
    except FleetTransportRefused as refused:
        # A name that does not resolve is refused too, and that is what a coordinator being restarted looks like.
        logger.error(
            "The coordinator's address could not be shown to be private; the option model inputs are not asked for",
            extra={"action": "option_model_inputs_coordinator_address_refused", "refusal": str(refused)},
        )
        raise CoordinatorDidNotAnswer(
            WhyNotKnown.COORDINATOR_ADDRESS_REFUSED,
            "the coordinator's address could not be shown to be inside the private network, so nothing was sent to it",
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
            WhyNotKnown.COORDINATOR_NOT_ASKED,
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
    except (httpx.InvalidURL, UnicodeError) as unusable:
        # An address no request can be made to, or an id or a token no header can carry.
        raise _not_set_up_to_ask(unusable) from unusable
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


def _not_set_up_to_ask(cause: Exception) -> CoordinatorDidNotAnswer:
    """What this Clerk was given to ask with cannot be used. The values themselves go nowhere, the log included."""
    logger.error(
        "This Clerk's coordinator address, id or token cannot be used to ask for the option model inputs",
        extra={"action": "option_model_inputs_coordinator_settings_unusable", "error_type": type(cause).__name__},
    )
    return CoordinatorDidNotAnswer(
        WhyNotKnown.COORDINATOR_NOT_CONFIGURED,
        "this Clerk is not set up to ask the coordinator: FLEET_COORDINATOR_URL is not an address a request can be "
        "made to, or FLEET_AGENT_SERVICE_TOKEN or FLEET_CLERK_ID cannot be sent",
    )


class AskedOfTheCoordinator:
    """One read's ask of the coordinator. Its answer holds both facts, so it is asked once, whichever fact's
    lookup comes for it first."""

    def __init__(self, underlying: str, at_ms: int, latest_expiry: date, wait: bool) -> None:
        self._asking = (underlying, at_ms, latest_expiry, wait)
        self._once = threading.Lock()
        self._heard: ModelInputsAnswer | ModelInputsBeingLookedUp | CoordinatorDidNotAnswer | None = None

    def _answer(self) -> ModelInputsAnswer | CoordinatorDidNotAnswer:
        underlying, at_ms, latest_expiry, wait = self._asking
        with self._once:
            if self._heard is None:
                try:
                    self._heard = ask_the_coordinator(underlying, at_ms=at_ms, latest_expiry=latest_expiry, wait=wait)
                except CoordinatorDidNotAnswer as silent:
                    self._heard = silent
            heard = self._heard
        if isinstance(heard, ModelInputsBeingLookedUp):
            raise ModelInputsBeingRead(f"the coordinator is looking up the rate and the dividends for {underlying}")
        return heard

    def rate(self, _days: int, _today: date) -> RateLearned:
        heard = self._answer()
        if isinstance(heard, CoordinatorDidNotAnswer):
            return the_default(str(heard), heard.why)
        return RateLearned(float(heard.rate.value), heard.rate.sentence, heard.rate.why_default)

    def dividends(self, underlying: str, _first: date, _last: date, at_ms: int) -> DividendsLearned:
        heard = self._answer()
        if isinstance(heard, CoordinatorDidNotAnswer):
            return DividendsLearned(None, f"{underlying}'s declared dividends are not known: {heard}.", at_ms, heard.why)
        said = heard.dividends
        if isinstance(said, DividendsNotKnown):
            return DividendsLearned(None, said.sentence, at_ms, said.why)
        going_ex = tuple(DividendOn(ex_ms=dividend.ex_ms, cash=float(dividend.cash)) for dividend in said.declared)
        return DividendsLearned(going_ex, said.sentence, said.read_ms, None)


__all__ = ["AskedOfTheCoordinator", "CoordinatorDidNotAnswer", "ask_the_coordinator"]
