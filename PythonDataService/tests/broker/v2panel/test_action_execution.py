"""Tests for presented-action execution (S1, spec §11).

Pins the three execution invariants: revision guard (stale → 409), idempotency
(re-post is a no-op), and identity-from-channel (never a request field).
"""

from __future__ import annotations

import pytest

from app.schemas.broker_v2_panel import PanelActionRequest
from app.services.broker_v2_panel.action_execution_service import (
    ActionNotAvailableError,
    IdempotencyStore,
    StaleRevisionError,
    execute_action,
)

_SID = "bot-alpha"


def _request(*, action_id: str = "stop", revision: int = 42, key: str = "k1") -> PanelActionRequest:
    return PanelActionRequest(
        action_id=action_id,  # type: ignore[arg-type]
        revision=revision,
        idempotency_key=key,
    )


async def test_action_applies_and_records_identity() -> None:
    seen_identity: list[str] = []

    async def _perform(operator: str) -> str:
        seen_identity.append(operator)
        return "stopped"

    store = IdempotencyStore()
    result = await execute_action(
        _request(),
        sid=_SID,
        current_revision=42,
        performers={"stop": _perform},
        operator_identity="desk-operator",
        store=store,
    )

    assert result.applied is True
    assert result.action_id == "stop"
    assert result.message == "stopped"
    # Identity came from the channel, not the request.
    assert seen_identity == ["desk-operator"]


async def test_stale_revision_is_409() -> None:
    async def _perform(operator: str) -> str:  # pragma: no cover - must not run
        raise AssertionError("performer must not run on a stale revision")

    with pytest.raises(StaleRevisionError) as exc:
        await execute_action(
            _request(revision=41),
            sid=_SID,
            current_revision=42,
            performers={"stop": _perform},
            operator_identity="op",
            store=IdempotencyStore(),
        )
    assert exc.value.http_status == 409


async def test_idempotent_repost_is_a_noop() -> None:
    calls = 0

    async def _perform(operator: str) -> str:
        nonlocal calls
        calls += 1
        return "stopped"

    store = IdempotencyStore()
    first = await execute_action(
        _request(key="dup"),
        sid=_SID,
        current_revision=42,
        performers={"stop": _perform},
        operator_identity="op",
        store=store,
    )
    second = await execute_action(
        _request(key="dup"),
        sid=_SID,
        current_revision=42,
        performers={"stop": _perform},
        operator_identity="op",
        store=store,
    )

    assert first.applied is True
    assert second.applied is False
    assert calls == 1


async def test_idempotent_repost_survives_revision_advance() -> None:
    """A retry of an applied action stays a no-op even after the panel advances."""
    async def _perform(operator: str) -> str:
        return "stopped"

    store = IdempotencyStore()
    await execute_action(
        _request(key="dup", revision=42),
        sid=_SID,
        current_revision=42,
        performers={"stop": _perform},
        operator_identity="op",
        store=store,
    )
    # The action already took effect; the panel revision moved on. A retry with
    # the SAME key must not 409 — it is a benign no-op.
    result = await execute_action(
        _request(key="dup", revision=42),
        sid=_SID,
        current_revision=99,
        performers={"stop": _perform},
        operator_identity="op",
        store=store,
    )
    assert result.applied is False


async def test_unwired_action_is_typed_not_available() -> None:
    with pytest.raises(ActionNotAvailableError) as exc:
        await execute_action(
            _request(action_id="retire"),
            sid=_SID,
            current_revision=42,
            performers={"stop": lambda op: _noop()},  # retire not wired
            operator_identity="op",
            store=IdempotencyStore(),
        )
    assert exc.value.http_status == 409


async def _noop() -> str:
    return "noop"
