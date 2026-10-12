"""The Options Backtest page's setup routes (#3030).

``GET /session-clock`` is a full session's open and close, from the calendar.
``POST /setups/describe`` says a setup in plain sentences. ``GET /limits`` is what a setup and a range may be. ``GET /setups``
lists the shipped examples and the saved setups, ``PUT /setups/{name}`` saves
one and ``DELETE /setups/{name}`` removes one. Nothing here runs a backtest.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Response, status

from app.options_backtest import saved_setups
from app.options_backtest.describe import describe_setup
from app.options_backtest.limits import MAX_RANGE_SESSIONS
from app.options_backtest.saved_setups import SETUP_NAME_PATTERN, SETUP_NAME_RULE, ExampleSetupError
from app.options_backtest.session_clock import SessionClock, full_session_clock
from app.options_backtest.setup import (
    MAX_CONTRACTS,
    MAX_EXPIRY_DAYS,
    MAX_LEGS,
    MAX_REENTRIES,
    MAX_STRIKE_STEPS,
    OptionsBacktestSetup,
)
from app.research.persistence.db import with_connection
from app.schemas.options_backtest import (
    OptionsBacktestLimitsResponse,
    OptionsBacktestRefusalBody,
    OptionsBacktestSetupNameRuleResponse,
    SavedSetupResponse,
    SessionClockResponse,
    SetupDescriptionResponse,
)
from app.utils.timestamps import now_ms_utc

router = APIRouter()

SetupName = Annotated[str, Path(pattern=SETUP_NAME_PATTERN, description=SETUP_NAME_RULE)]

_IS_AN_EXAMPLE = {status.HTTP_409_CONFLICT: {"model": OptionsBacktestRefusalBody}}


async def _full_session_clock() -> SessionClock:
    # The first read of a calendar quarter builds its schedule: off the event loop so no other request waits on it.
    return await asyncio.to_thread(full_session_clock, as_of_ms=now_ms_utc())


@router.get("/session-clock", response_model=SessionClockResponse)
async def get_session_clock() -> SessionClockResponse:
    """A full session's open and close as minutes past New York midnight, from the calendar."""
    return SessionClockResponse.from_clock(await _full_session_clock())


@router.post("/setups/describe", response_model=SetupDescriptionResponse)
async def describe(setup: OptionsBacktestSetup) -> SetupDescriptionResponse:
    """The setup in sentences, and the setup as the server read it. A setup that does not validate is the standard 422."""
    description = describe_setup(setup, full_session=await _full_session_clock())
    return SetupDescriptionResponse.from_description(setup, description)


@router.get("/limits", response_model=OptionsBacktestLimitsResponse)
async def get_limits() -> OptionsBacktestLimitsResponse:
    """What a setup may hold (legs, contracts, re-entries, strike steps, expiry days), what a name may be, and how long a range may be."""
    return OptionsBacktestLimitsResponse(
        max_legs=MAX_LEGS,
        max_contracts_per_leg=MAX_CONTRACTS,
        max_reentries_per_leg=MAX_REENTRIES,
        max_strike_steps=MAX_STRIKE_STEPS,
        max_expiry_days=MAX_EXPIRY_DAYS,
        max_range_sessions=MAX_RANGE_SESSIONS,
        setup_name=OptionsBacktestSetupNameRuleResponse(pattern=SETUP_NAME_PATTERN, sentence=SETUP_NAME_RULE),
    )


@router.get("/setups", response_model=list[SavedSetupResponse])
async def list_setups() -> list[SavedSetupResponse]:
    """The two shipped examples, then the saved setups, newest first. One that cannot be opened says why."""
    listed = await with_connection(saved_setups.list_setups)
    return [SavedSetupResponse.from_saved(saved) for saved in listed]


@router.put("/setups/{name}", response_model=SavedSetupResponse, responses=_IS_AN_EXAMPLE)
async def save_setup(name: SetupName, setup: OptionsBacktestSetup) -> SavedSetupResponse:
    """Save the setup under the name, replacing what was saved under it. An example's name is a 409."""
    try:
        saved = await with_connection(saved_setups.save_setup, name, setup, saved_at_ms=now_ms_utc())
    except ExampleSetupError as exc:
        raise _is_an_example(exc) from exc
    return SavedSetupResponse.from_saved(saved)


@router.delete(
    "/setups/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**_IS_AN_EXAMPLE, status.HTTP_404_NOT_FOUND: {"model": OptionsBacktestRefusalBody}},
)
async def delete_setup(name: SetupName) -> Response:
    """Delete the saved setup. None by that name is a 404, and an example's name is a 409."""
    try:
        deleted = await with_connection(saved_setups.delete_setup, name)
    except ExampleSetupError as exc:
        raise _is_an_example(exc) from exc
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "OPTIONS_BACKTEST_SETUP_NOT_FOUND", "message": f"No saved setup is named '{name}'."},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _is_an_example(error: ExampleSetupError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "OPTIONS_BACKTEST_SETUP_IS_AN_EXAMPLE", "message": str(error)},
    )
