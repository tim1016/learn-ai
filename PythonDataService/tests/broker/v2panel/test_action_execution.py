"""Tests for presented-action execution (S1, spec §11).

Pins the three execution invariants: revision guard (stale → 409), idempotency
(re-post is a no-op), and identity-from-channel (never a request field).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.schemas.broker_v2_panel import PanelActionRequest, PanelActionResult
from app.services.broker_v2_panel.action_execution_service import (
    ActionNotAvailableError,
    DurableIdempotencyStore,
    IdempotencyStore,
    StaleRevisionError,
    execute_action,
)
from app.services.broker_v2_panel.panel_data_source import _action_performers

_SID = "bot-alpha"


def _request(
    *, action_id: str = "stop", revision: int = 42, key: str = "k1", token: str = "token"
) -> PanelActionRequest:
    return PanelActionRequest(
        action_id=action_id,  # type: ignore[arg-type]
        revision=revision,
        concurrency_token=token,
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
        current_concurrency_token="token",
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
            _request(token="stale-token"),
            sid=_SID,
            current_revision=42,
            current_concurrency_token="token",
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
        current_concurrency_token="token",
        performers={"stop": _perform},
        operator_identity="op",
        store=store,
    )
    second = await execute_action(
        _request(key="dup"),
        sid=_SID,
        current_revision=42,
        current_concurrency_token="token",
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
        current_concurrency_token="token",
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
        current_concurrency_token="token",
        performers={"stop": _perform},
        operator_identity="op",
        store=store,
    )
    assert result.applied is False


async def test_unrelated_panel_revision_does_not_stale_an_action_token() -> None:
    """A new receipt may refresh the panel while an unchanged STOP stays valid."""
    result = await execute_action(
        _request(revision=1),
        sid=_SID,
        current_revision=99,
        current_concurrency_token="token",
        performers={"stop": lambda _operator: _noop()},
        operator_identity="op",
        store=IdempotencyStore(),
    )
    assert result.applied is True


async def test_durable_receipt_prevents_reexecution_after_store_restart(tmp_path: Path) -> None:
    calls = 0

    async def _perform(_operator: str) -> str:
        nonlocal calls
        calls += 1
        return "stopped"

    path = tmp_path / "panel_action_receipts.json"
    first = await execute_action(
        _request(key="durable"),
        sid=_SID,
        current_revision=42,
        current_concurrency_token="token",
        performers={"stop": _perform},
        operator_identity="op",
        store=DurableIdempotencyStore(path),
    )
    replay = await execute_action(
        _request(key="durable"),
        sid=_SID,
        current_revision=99,
        current_concurrency_token="token",
        performers={"stop": _perform},
        operator_identity="op",
        store=DurableIdempotencyStore(path),
    )
    assert first.applied is True
    assert replay.applied is False
    assert calls == 1


async def test_unwired_action_is_typed_not_available() -> None:
    with pytest.raises(ActionNotAvailableError) as exc:
        await execute_action(
            _request(action_id="retire"),
            sid=_SID,
            current_revision=42,
            current_concurrency_token="token",
            performers={"stop": lambda op: _noop()},  # retire not wired
            operator_identity="op",
            store=IdempotencyStore(),
        )
    assert exc.value.http_status == 409


async def test_start_performer_resumes_durable_binding(monkeypatch) -> None:
    resumed: list[tuple[str, str]] = []

    class _Registry:
        async def resume_existing(self, broker: str, sid: str) -> None:
            resumed.append((broker, sid))

    monkeypatch.setattr(
        "app.services.broker_v2_panel.panel_data_source.get_bot_task_registry",
        lambda: _Registry(),
    )

    message = await _action_performers(
        "alpaca", _SID, idempotency_key="resume-1"
    )["start"]("desk-operator")

    assert resumed == [("alpaca", _SID)]
    assert message == (
        "Bot started from its durable deployment configuration. "
        "The Clerk remains the only owner of broker order effects."
    )


async def _noop() -> str:
    return "noop"


async def test_stale_revision_releases_key_for_corrected_retry() -> None:
    """A stale-revision rejection must not strand the idempotency key.

    The action never ran, so the key must be reusable: the pre-execution
    rejection frees the reservation instead of leaving it ``in_flight``, and a
    corrected retry (with the current revision) executes.
    """
    calls = 0

    async def _perform(operator: str) -> str:
        nonlocal calls
        calls += 1
        return "stopped"

    store = IdempotencyStore()

    with pytest.raises(StaleRevisionError):
        await execute_action(
            _request(key="retry", token="stale-token"),
            sid=_SID,
            current_revision=42,
            current_concurrency_token="token",
            performers={"stop": _perform},
            operator_identity="op",
            store=store,
        )

    # Nothing was applied → no dangling in_flight reservation for the key.
    assert (_SID, "stop", "retry") not in store._records

    result = await execute_action(
        _request(key="retry", revision=42),
        sid=_SID,
        current_revision=42,
        current_concurrency_token="token",
        performers={"stop": _perform},
        operator_identity="op",
        store=store,
    )
    assert result.applied is True
    assert calls == 1


async def test_not_available_action_releases_key() -> None:
    """An unwired action rejects pre-execution and frees its idempotency key."""
    store = IdempotencyStore()

    with pytest.raises(ActionNotAvailableError):
        await execute_action(
            _request(action_id="retire", key="na"),
            sid=_SID,
            current_revision=42,
            current_concurrency_token="token",
            performers={"stop": lambda op: _noop()},  # retire not wired
            operator_identity="op",
            store=store,
        )

    assert (_SID, "retire", "na") not in store._records


async def test_duplicate_concurrent_posts_run_mutation_once() -> None:
    """Two concurrent POSTs with the same idempotency_key must fire the mutation once."""
    calls = 0

    async def _slow_perform(operator: str) -> str:
        nonlocal calls
        calls += 1
        # Yield briefly so the second coroutine can reach reserve_or_get before we finish.
        await asyncio.sleep(0)
        return "stopped"

    store = IdempotencyStore()

    async def _post() -> PanelActionResult | None:
        try:
            return await execute_action(
                _request(key="race"),
                sid=_SID,
                current_revision=42,
                current_concurrency_token="token",
                performers={"stop": _slow_perform},
                operator_identity="op",
                store=store,
            )
        except Exception:
            return None

    results = await asyncio.gather(_post(), _post())
    applied_count = sum(1 for r in results if r is not None and r.applied)
    noop_count = sum(1 for r in results if r is not None and not r.applied)
    assert calls == 1, f"mutation ran {calls} times, expected 1"
    # One result is applied=True, one is applied=False (or both may be True if
    # the second started before the first completed and fell through — but calls
    # must still be 1 because the first call completed and set the record before
    # the second performer could start).
    assert applied_count + noop_count == 2
