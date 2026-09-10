"""User-owned broker configuration profiles (transport only).

Owns the literal prefix ``/api/brokers/alpaca/configuration``. ``configuration``
is not a claimed depth-2 segment under the existing ``/api/brokers/{broker}/…``
wildcard routes; ``tests/contracts/test_broker_configuration_route_prefix.py``
pins that rather than leaving it to registration order.

Every route validates and parses the request, calls the configuration service,
and shapes the response. No rule about what a write *means* lives here — those
are in ``app/broker_configuration/``. A typed refusal is translated once, by
:func:`broker_configuration_exception_handler`, in the Clerk's own precedent:
a configuration refusal is a state of the system, not an unexpected fault, so
it must never fall through to the catch-all 500.

Auth is the existing control-plane posture, not a new mechanism: the
``X-Data-Plane-Control-Secret`` header, checked always on reads
(``require_data_plane_control_secret_always``) and on mutations
(``require_data_plane_control_secret``), with
``DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL`` unchanged.

Nothing here restarts a container, changes a runtime, or grants broker
authority. ``POST /selection/apply`` records intent and returns ``202``; the
staged revision becomes effective at the next controlled restart, which the
operator performs.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse

from app.broker_configuration.envelope import ValidatedLiveEnvelope
from app.broker_configuration.errors import BrokerConfigurationError
from app.broker_configuration.records import ProfileWithRevision
from app.broker_configuration.runtime import get_broker_configuration_service
from app.broker_configuration.service import BrokerConfigurationService, live_envelope_from_request
from app.schemas.broker_configuration import (
    SERVER_RESOLVED_FIELDS,
    AccountPinRequest,
    AccountVerificationResponse,
    ApplyRequest,
    ConfigurationEventListResponse,
    ConfigurationEventResponse,
    CredentialSlotResponse,
    CredentialSlotsResponse,
    NicknameListResponse,
    NicknamePutRequest,
    NicknameResponse,
    ObservedAccountResponse,
    OwnerPatchRequest,
    OwnerResponse,
    ProfileCloneRequest,
    ProfileCreateRequest,
    ProfileDetailResponse,
    ProfileListResponse,
    ProfilePatchRequest,
    ProfileResponse,
    RevisionContentRequest,
    RevisionCreateRequest,
    RevisionListResponse,
    RevisionResponse,
    SelectionPutRequest,
    SelectionResponse,
)
from app.security.data_plane_control import (
    require_data_plane_control_secret,
    require_data_plane_control_secret_always,
)

logger = logging.getLogger(__name__)

PREFIX = "/api/brokers/alpaca/configuration"
MAX_EVENT_LIMIT = 200


async def broker_configuration_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Answer a configuration refusal in the contract's own vocabulary."""
    del request
    if not isinstance(exc, BrokerConfigurationError):  # pragma: no cover - registration guard
        raise exc
    logger.info(
        "Broker configuration request refused",
        extra={"reason": exc.reason, "status_code": exc.status_code},
    )
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail()})


async def refuse_server_resolved_fields(request: Request) -> None:
    """Refuse a body that claims an identity the server resolves.

    Owner and actor come from the local-owner record, never from a request
    (ADR 0060 Decision 3). Silently dropping the field would let a client
    believe it had set something, so this is an explicit ``422`` carrying the
    contract's ``owner_field_not_accepted`` reason rather than the generic shape
    ``extra="forbid"`` produces.
    """
    if request.method.upper() not in {"POST", "PUT", "PATCH"}:
        return
    try:
        body = await request.json()
    except ValueError:
        # Malformed or absent JSON: the body model's own 422 says it better.
        return
    if not isinstance(body, dict):
        return
    claimed = sorted(field for field in SERVER_RESOLVED_FIELDS if field in body)
    if claimed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "reason": "owner_field_not_accepted",
                "message": "This request named " + ", ".join(claimed) + ", which the server resolves.",
                "next_step": "Remove those fields and send the request again.",
            },
        )


READ_DEPENDENCIES = [Depends(require_data_plane_control_secret_always)]
WRITE_DEPENDENCIES = [
    Depends(require_data_plane_control_secret),
    Depends(refuse_server_resolved_fields),
]

router = APIRouter(prefix=PREFIX, tags=["broker-configuration"])

ServiceDep = Annotated[BrokerConfigurationService, Depends(get_broker_configuration_service)]
ProfileId = Annotated[str, Path(min_length=1, max_length=120)]
AccountId = Annotated[str, Path(min_length=1, max_length=120)]
RevisionNumber = Annotated[int, Path(ge=1)]


def _envelope(content: RevisionContentRequest) -> ValidatedLiveEnvelope | None:
    """Request data becomes an envelope only through the validated type."""
    payload = None if content.live_envelope is None else content.live_envelope.model_dump()
    return live_envelope_from_request(payload)


def _detail(record: ProfileWithRevision) -> ProfileDetailResponse:
    return ProfileDetailResponse(
        profile=ProfileResponse.from_record(record.profile),
        latest_revision=(
            None
            if record.latest_revision is None
            else RevisionResponse.from_record(record.latest_revision)
        ),
    )


# ---- owner ---------------------------------------------------------------


@router.get("/owner", response_model=OwnerResponse, dependencies=READ_DEPENDENCIES)
async def read_owner(service: ServiceDep) -> OwnerResponse:
    return OwnerResponse.from_record(service.owner())


@router.patch("/owner", response_model=OwnerResponse, dependencies=WRITE_DEPENDENCIES)
async def patch_owner(service: ServiceDep, body: OwnerPatchRequest) -> OwnerResponse:
    return OwnerResponse.from_record(service.rename_owner(display_label=body.display_label))


# ---- credential slots ----------------------------------------------------


@router.get(
    "/credential-slots",
    response_model=CredentialSlotsResponse,
    dependencies=READ_DEPENDENCIES,
)
async def list_credential_slots(service: ServiceDep) -> CredentialSlotsResponse:
    """Slot labels and availability. Never a value, a fragment, or a var name."""
    return CredentialSlotsResponse(
        slots=tuple(CredentialSlotResponse.from_record(slot) for slot in service.credential_slots())
    )


# ---- profiles ------------------------------------------------------------


@router.get("/profiles", response_model=ProfileListResponse, dependencies=READ_DEPENDENCIES)
async def list_profiles(
    service: ServiceDep,
    include_archived: bool = Query(default=False),
) -> ProfileListResponse:
    profiles = service.list_profiles(include_archived=include_archived)
    return ProfileListResponse(
        profiles=tuple(ProfileResponse.from_record(profile) for profile in profiles)
    )


@router.post(
    "/profiles",
    response_model=ProfileDetailResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=WRITE_DEPENDENCIES,
)
async def create_profile(service: ServiceDep, body: ProfileCreateRequest) -> ProfileDetailResponse:
    return _detail(
        service.create_profile(
            display_name=body.display_name,
            credential_slot=body.credential_slot,
            endpoint_mode=body.endpoint_mode,
            live_envelope=_envelope(body),
        )
    )


@router.get(
    "/profiles/{profile_id}",
    response_model=ProfileDetailResponse,
    dependencies=READ_DEPENDENCIES,
)
async def read_profile(service: ServiceDep, profile_id: ProfileId) -> ProfileDetailResponse:
    return _detail(service.read_profile(profile_id))


@router.patch(
    "/profiles/{profile_id}",
    response_model=ProfileResponse,
    dependencies=WRITE_DEPENDENCIES,
)
async def patch_profile(
    service: ServiceDep, profile_id: ProfileId, body: ProfilePatchRequest
) -> ProfileResponse:
    """Metadata only — ``display_name`` and ``archived``."""
    updated = service.update_profile(
        profile_id, display_name=body.display_name, archived=body.archived
    )
    return ProfileResponse.from_record(updated)


@router.post(
    "/profiles/{profile_id}/clone",
    response_model=ProfileDetailResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=WRITE_DEPENDENCIES,
)
async def clone_profile(
    service: ServiceDep, profile_id: ProfileId, body: ProfileCloneRequest
) -> ProfileDetailResponse:
    """New profile, copied content, no account pin carried over."""
    return _detail(service.clone_profile(profile_id, display_name=body.display_name))


# ---- revisions -----------------------------------------------------------


@router.get(
    "/profiles/{profile_id}/revisions",
    response_model=RevisionListResponse,
    dependencies=READ_DEPENDENCIES,
)
async def list_revisions(service: ServiceDep, profile_id: ProfileId) -> RevisionListResponse:
    revisions = service.list_revisions(profile_id)
    return RevisionListResponse(
        revisions=tuple(RevisionResponse.from_record(revision) for revision in revisions)
    )


@router.post(
    "/profiles/{profile_id}/revisions",
    response_model=RevisionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=WRITE_DEPENDENCIES,
)
async def create_revision(
    service: ServiceDep, profile_id: ProfileId, body: RevisionCreateRequest
) -> RevisionResponse:
    revision = service.create_revision(
        profile_id,
        expected_revision=body.expected_revision,
        credential_slot=body.credential_slot,
        endpoint_mode=body.endpoint_mode,
        live_envelope=_envelope(body),
    )
    return RevisionResponse.from_record(revision)


@router.get(
    "/profiles/{profile_id}/revisions/{revision}",
    response_model=RevisionResponse,
    dependencies=READ_DEPENDENCIES,
)
async def read_revision(
    service: ServiceDep, profile_id: ProfileId, revision: RevisionNumber
) -> RevisionResponse:
    return RevisionResponse.from_record(service.read_revision(profile_id, revision))


@router.post(
    "/profiles/{profile_id}/revisions/{revision}/verify-account",
    response_model=AccountVerificationResponse,
    dependencies=WRITE_DEPENDENCIES,
)
async def verify_account(
    service: ServiceDep, profile_id: ProfileId, revision: RevisionNumber
) -> AccountVerificationResponse:
    """Read-only broker account discovery. Never submits or cancels anything."""
    observed = await service.verify_account(profile_id, revision)
    return AccountVerificationResponse(
        observed_accounts=tuple(ObservedAccountResponse.from_record(account) for account in observed)
    )


@router.post(
    "/profiles/{profile_id}/revisions/{revision}/account-pin",
    response_model=RevisionResponse,
    dependencies=WRITE_DEPENDENCIES,
)
async def pin_account(
    service: ServiceDep,
    profile_id: ProfileId,
    revision: RevisionNumber,
    body: AccountPinRequest,
) -> RevisionResponse:
    """Pin one explicitly selected observed account to this revision."""
    pinned = await service.pin_account(profile_id, revision, account_id=body.account_id)
    return RevisionResponse.from_record(pinned)


# ---- account nicknames ---------------------------------------------------


@router.get(
    "/account-nicknames",
    response_model=NicknameListResponse,
    dependencies=READ_DEPENDENCIES,
)
async def list_nicknames(service: ServiceDep) -> NicknameListResponse:
    nicknames = service.list_nicknames()
    return NicknameListResponse(
        nicknames=tuple(NicknameResponse.from_record(nickname) for nickname in nicknames)
    )


@router.put(
    "/account-nicknames/{account_id}",
    response_model=NicknameResponse,
    dependencies=WRITE_DEPENDENCIES,
)
async def put_nickname(
    service: ServiceDep, account_id: AccountId, body: NicknamePutRequest
) -> NicknameResponse:
    return NicknameResponse.from_record(service.set_nickname(account_id, nickname=body.nickname))


# ---- installation selection ---------------------------------------------


@router.get("/selection", response_model=SelectionResponse, dependencies=READ_DEPENDENCIES)
async def read_selection(service: ServiceDep) -> SelectionResponse:
    """Staged and effective, always both, so neither can be rendered as the other."""
    return SelectionResponse.from_record(service.selection())


@router.put("/selection", response_model=SelectionResponse, dependencies=WRITE_DEPENDENCIES)
async def put_selection(service: ServiceDep, body: SelectionPutRequest) -> SelectionResponse:
    """Stage an exact profile revision. Staging is not switching."""
    staged = service.stage_selection(
        profile_id=body.profile_id,
        revision=body.revision,
        expected_selection_generation=body.expected_selection_generation,
    )
    return SelectionResponse.from_record(staged)


@router.post(
    "/selection/apply",
    response_model=SelectionResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=WRITE_DEPENDENCIES,
)
async def apply_selection(service: ServiceDep, body: ApplyRequest) -> SelectionResponse:
    """Record the one-shot Apply. Changes no runtime; 202, never 200."""
    requested = service.request_apply(
        expected_selection_generation=body.expected_selection_generation
    )
    return SelectionResponse.from_record(requested)


# ---- audit ---------------------------------------------------------------


@router.get(
    "/events",
    response_model=ConfigurationEventListResponse,
    dependencies=READ_DEPENDENCIES,
)
async def list_events(
    service: ServiceDep,
    limit: int = Query(default=50, ge=1, le=MAX_EVENT_LIMIT),
    before_event_id: str | None = Query(default=None, min_length=1, max_length=120),
) -> ConfigurationEventListResponse:
    events = service.events(limit=limit, before_event_id=before_event_id)
    return ConfigurationEventListResponse(
        events=tuple(ConfigurationEventResponse.from_record(event) for event in events)
    )


__all__ = [
    "PREFIX",
    "broker_configuration_exception_handler",
    "refuse_server_resolved_fields",
    "router",
]
