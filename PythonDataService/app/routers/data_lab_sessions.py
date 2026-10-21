"""Saved Data Lab sessions over REST (``/api/data-lab/sessions``).

Replaces the GraphQL ``dataLabSessions`` / ``dataLabSession`` queries and the save, update, rename and delete
mutations of the retired .NET backend. The mutations used to answer ``{success, id, message}`` in-band; here a missing
session is a 404 and a malformed body a 422.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.routers.research_store_unavailable import StoreUnreachableIsA503
from app.schemas.data_lab_sessions import (
    DataLabSessionReceipt,
    DataLabSessionRefusalBody,
    DataLabSessionRenameRequest,
    DataLabSessionRequest,
    DataLabSessionResponse,
)
from app.services.data_lab_session_service import DataLabSessionStore, PostgresDataLabSessionStore


class _DataLabStoreUnreachableIsA503(StoreUnreachableIsA503):
    unavailable_detail = {
        "code": "DATA_LAB_SESSIONS_STORE_UNAVAILABLE",
        "message": "The saved Data Lab sessions could not be reached. Try again once the database is back.",
    }


router = APIRouter(route_class=_DataLabStoreUnreachableIsA503)

_NOT_FOUND = {status.HTTP_404_NOT_FOUND: {"model": DataLabSessionRefusalBody}}


def get_session_store() -> DataLabSessionStore:
    return PostgresDataLabSessionStore()


SessionStore = Annotated[DataLabSessionStore, Depends(get_session_store)]


def _not_found(session_id: UUID) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "DATA_LAB_SESSION_NOT_FOUND", "message": f"Session {session_id} not found"},
    )


@router.get("", response_model=list[DataLabSessionResponse])
async def list_data_lab_sessions(store: SessionStore) -> list[DataLabSessionResponse]:
    """Every saved session, most recently updated first, each whole (chart snapshot included)."""
    return await store.list_sessions()


@router.get("/{session_id}", response_model=DataLabSessionResponse, responses=_NOT_FOUND)
async def get_data_lab_session(session_id: UUID, store: SessionStore) -> DataLabSessionResponse:
    session = await store.get_session(session_id)
    if session is None:
        raise _not_found(session_id)
    return session


@router.post("", response_model=DataLabSessionReceipt, status_code=status.HTTP_201_CREATED)
async def save_data_lab_session(body: DataLabSessionRequest, store: SessionStore) -> DataLabSessionReceipt:
    """Save a new session. The server picks its id and stamps its instants."""
    return await store.create_session(body)


@router.put("/{session_id}", response_model=DataLabSessionReceipt, responses=_NOT_FOUND)
async def replace_data_lab_session(
    session_id: UUID, body: DataLabSessionRequest, store: SessionStore
) -> DataLabSessionReceipt:
    """Replace a session whole: name, configuration, entries and chart snapshot. Its creation instant is kept."""
    receipt = await store.replace_session(session_id, body)
    if receipt is None:
        raise _not_found(session_id)
    return receipt


@router.patch("/{session_id}/name", response_model=DataLabSessionReceipt, responses=_NOT_FOUND)
async def rename_data_lab_session(
    session_id: UUID, body: DataLabSessionRenameRequest, store: SessionStore
) -> DataLabSessionReceipt:
    receipt = await store.rename_session(session_id, body.name)
    if receipt is None:
        raise _not_found(session_id)
    return receipt


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_NOT_FOUND)
async def delete_data_lab_session(session_id: UUID, store: SessionStore) -> Response:
    if not await store.delete_session(session_id):
        raise _not_found(session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
