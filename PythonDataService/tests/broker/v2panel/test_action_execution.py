"""Tests for presented-action execution (S1, spec §11).

Pins the three execution invariants: revision guard (stale → 409), idempotency
(re-post is a no-op), and identity-from-channel (never a request field).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.alpaca.clerk.models import EffectOperationState
from app.schemas.broker_v2_panel import PanelActionRequest, PanelActionResult
from app.schemas.run_admission import RunAdmissionDecision, RunAdmissionFactAges
from app.services.bot_runner_errors import RunAdmissionRefusedError
from app.services.broker_v2_panel import panel_data_source
from app.services.broker_v2_panel.action_execution_service import (
    ActionNotAvailableError,
    ActionOutcomeUnknownError,
    DurableIdempotencyStore,
    IdempotencyStore,
    StaleRevisionError,
    UnknownActionError,
    execute_action,
)
from app.services.broker_v2_panel.panel_data_source import _action_performers, run_action
from app.services.broker_v2_panel.sqlite_panel_source import execute_sqlite_panel_action

_SID = "bot-alpha"


def _request(
    *,
    action_id: str = "stop",
    revision: int = 42,
    key: str = "k1",
    token: str = "token",
    reason: str | None = None,
) -> PanelActionRequest:
    return PanelActionRequest(
        action_id=action_id,  # type: ignore[arg-type]
        revision=revision,
        concurrency_token=token,
        idempotency_key=key,
        reason=reason,
    )


async def test_action_applies_and_records_identity() -> None:
    seen_identity: list[str] = []

    async def _perform(operator: str, reason: str | None) -> str:
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
    assert result.outcome == "success"
    assert result.receipt_id == "k1"
    assert result.recorded_at_ms > 0
    assert result.action_id == "stop"
    assert result.message == "stopped"
    # Identity came from the channel, not the request.
    assert seen_identity == ["desk-operator"]


async def test_execute_action_forwards_request_reason_to_performer() -> None:
    """The executor threads the request's operator-authored reason to the performer.

    Identity is channel-derived (never a request field); reason is the one
    request-authored value the performer receives alongside it.
    """
    seen: list[tuple[str, str | None]] = []

    async def _perform(operator: str, reason: str | None) -> str:
        seen.append((operator, reason))
        return "stopped"

    store = IdempotencyStore()
    await execute_action(
        _request(key="reason-thread", reason="operator note"),
        sid=_SID,
        current_revision=42,
        current_concurrency_token="token",
        performers={"stop": _perform},
        operator_identity="desk-operator",
        store=store,
    )

    assert seen == [("desk-operator", "operator note")]


async def test_stale_revision_is_409() -> None:
    async def _perform(operator: str, reason: str | None) -> str:  # pragma: no cover - must not run
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

    async def _perform(operator: str, reason: str | None) -> str:
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
    assert second.receipt_id == first.receipt_id
    assert second.recorded_at_ms == first.recorded_at_ms
    assert calls == 1


async def test_performer_failure_returns_unknown_and_burns_receipt_key() -> None:
    async def _perform(_operator: str, _reason: str | None) -> str:
        raise RuntimeError("connection dropped after dispatch")

    store = IdempotencyStore()
    with pytest.raises(ActionOutcomeUnknownError) as exc:
        await execute_action(
            _request(key="unknown"),
            sid=_SID,
            current_revision=42,
            current_concurrency_token="token",
            performers={"stop": _perform},
            operator_identity="op",
            store=store,
        )

    assert "Inspect Clerk evidence" in (exc.value.detail or "")
    assert store._records[(_SID, "stop", "unknown")].state == "failed"


async def test_performer_raised_action_execution_error_burns_key_not_released(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A performer-raised ``ActionExecutionError`` subclass is a performer failure.

    Only ``StaleRevisionError``/``ActionNotAvailableError`` raised BEFORE the
    performer runs are pre-execution rejections. A performer that starts real
    work and then raises some other ``ActionExecutionError`` subclass (here
    ``UnknownActionError``, standing in for any future performer-raised type)
    must be treated like any other performer failure: the key is failed (not
    released) and no ``panel_action_rejected`` pre-execution log fires.
    """

    async def _perform(_operator: str, _reason: str | None) -> str:
        raise UnknownActionError("performer rejected mid-flight")

    store = IdempotencyStore()
    with caplog.at_level(logging.INFO), pytest.raises(ActionOutcomeUnknownError):
        await execute_action(
            _request(key="mid-flight"),
            sid=_SID,
            current_revision=42,
            current_concurrency_token="token",
            performers={"stop": _perform},
            operator_identity="op",
            store=store,
        )

    assert store._records[(_SID, "stop", "mid-flight")].state == "failed"
    assert "panel_action_rejected" not in caplog.text


async def test_disabled_presented_action_cannot_bypass_guard_via_post(
    monkeypatch,
    tmp_path: Path,
) -> None:
    async def _panel(*_args, **_kwargs):
        return SimpleNamespace(
            revision=7,
            actions=[
                SimpleNamespace(
                    action_id="stop",
                    label="Stop",
                    enabled=False,
                    blockers=[SimpleNamespace(detail="Start the bot before Stop.")],
                    concurrency_token="token",
                )
            ],
        )

    monkeypatch.setattr("app.services.broker_v2_panel.panel_data_source.get_panel", _panel)
    monkeypatch.setattr(
        "app.services.broker_v2_panel.panel_data_source.get_bot_task_registry",
        lambda: SimpleNamespace(
            panel_action_receipt_path=lambda _sid: tmp_path / "receipts.json"
        ),
    )

    with pytest.raises(ActionNotAvailableError) as exc:
        await run_action(
            "alpaca",
            "account-1",
            _SID,
            _request(revision=7),
            operator_identity="operator",
        )

    assert exc.value.detail == "Start the bot before Stop."


async def test_sqlite_panel_resume_defers_to_lifecycle_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1410: Resume is lifecycle work, not a SQLite recovery capability."""
    monkeypatch.setattr(
        "app.services.broker_v2_panel.sqlite_panel_source.active_sqlite_facade",
        lambda _broker: object(),
    )

    result = await execute_sqlite_panel_action(
        "alpaca",
        "account-1",
        _SID,
        request=_request(action_id="resume"),
        panel=SimpleNamespace(revision=42),
        action=SimpleNamespace(concurrency_token="token"),
        availability_error=None,
    )

    assert result is None


async def test_idempotent_repost_survives_revision_advance() -> None:
    """A retry of an applied action stays a no-op even after the panel advances."""

    async def _perform(operator: str, reason: str | None) -> str:
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
        performers={"stop": lambda _operator, _reason: _noop()},
        operator_identity="op",
        store=IdempotencyStore(),
    )
    assert result.applied is True


async def test_durable_receipt_prevents_reexecution_after_store_restart(tmp_path: Path) -> None:
    calls = 0

    async def _perform(_operator: str, _reason: str | None) -> str:
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


async def test_legacy_durable_success_receipt_upgrades_without_reexecution(
    tmp_path: Path,
) -> None:
    path = tmp_path / "panel_action_receipts.json"
    compound = "\u001f".join((_SID, "stop", "legacy"))
    path.write_text(
        json.dumps(
            {
                compound: {
                    "state": "succeeded",
                    "result": {
                        "action_id": "stop",
                        "applied": True,
                        "revision": 42,
                        "concurrency_token": "token",
                        "message": "stopped",
                    },
                    "error_detail": None,
                }
            }
        ),
        encoding="utf-8",
    )
    legacy_observed_at_ms = path.stat().st_mtime_ns // 1_000_000

    result = await execute_action(
        _request(key="legacy"),
        sid=_SID,
        current_revision=99,
        current_concurrency_token="token",
        performers={"stop": lambda _operator, _reason: _noop()},
        operator_identity="op",
        store=DurableIdempotencyStore(path),
    )

    assert result.applied is False
    assert result.receipt_id == "legacy"
    assert result.recorded_at_ms == legacy_observed_at_ms


async def test_unwired_action_is_typed_not_available() -> None:
    with pytest.raises(ActionNotAvailableError) as exc:
        await execute_action(
            _request(action_id="retire"),
            sid=_SID,
            current_revision=42,
            current_concurrency_token="token",
            performers={"stop": lambda op, reason: _noop()},  # retire not wired
            operator_identity="op",
            store=IdempotencyStore(),
        )
    assert exc.value.http_status == 409


async def test_resume_performer_creates_new_run_from_durable_binding(monkeypatch) -> None:
    resumed: list[tuple[str, str]] = []

    class _Registry:
        async def resume_existing_with_admission(self, broker: str, sid: str):
            resumed.append((broker, sid))
            return SimpleNamespace(bot=SimpleNamespace(active_run_id="run-2"))

    monkeypatch.setattr(
        "app.services.broker_v2_panel.panel_data_source.get_bot_task_registry",
        lambda: _Registry(),
    )
    message = await _action_performers(
        "alpaca",
        _SID,
        idempotency_key="resume-1",
    )["resume"]("desk-operator", None)

    assert resumed == [("alpaca", _SID)]
    assert message == (
        "Bot resumed as new run run-2 from its immutable configuration. "
        "The Clerk remains the only owner of broker order effects."
    )


async def test_resume_performer_returns_known_refusal_when_fresh_admission_changes(monkeypatch) -> None:
    class _Registry:
        async def resume_existing_with_admission(self, _broker: str, _sid: str):
            raise RunAdmissionRefusedError(
                "Resume admission was refused.",
                detail="The Clerk evidence changed before activation.",
            )

    monkeypatch.setattr(
        "app.services.broker_v2_panel.panel_data_source.get_bot_task_registry",
        lambda: _Registry(),
    )

    with pytest.raises(ActionNotAvailableError) as exc:
        await _action_performers("alpaca", _SID, idempotency_key="resume-2")["resume"](
            "desk-operator", None
        )

    assert exc.value.http_status == 409
    assert exc.value.reason_code is None
    assert exc.value.detail == "The Clerk evidence changed before activation."


async def test_resume_performer_carries_the_admission_reason_code(monkeypatch) -> None:
    """PRD #1716 FR-4: an admission denial's reason_code survives the
    bot-runner-to-action mapping so the router/frontend can render it."""
    decision = RunAdmissionDecision(
        operation="RESUME",
        allowed=False,
        reason_code="TERMINAL_EVIDENCE_UNREADABLE",
        explanation="The terminal receipt could not be read.",
        next_step="This requires engineering investigation; Refresh to check for updated evidence.",
        strategy_instance_id=_SID,
        proposed_run_id="run-2",
        configuration_hash="a" * 64,
        account_id="paper-account",
        evaluated_at_ms=1_000,
        fact_ages_ms=RunAdmissionFactAges(runtime=0, process=0, market_data=0, market_liveness=0, clerk=0),
        evidence_refs=(),
    )

    class _Registry:
        async def resume_existing_with_admission(self, _broker: str, _sid: str):
            raise RunAdmissionRefusedError(
                "Resume admission was refused.",
                detail=decision.explanation,
                admission_decision=decision,
            )

    monkeypatch.setattr(
        "app.services.broker_v2_panel.panel_data_source.get_bot_task_registry",
        lambda: _Registry(),
    )

    with pytest.raises(ActionNotAvailableError) as exc:
        await _action_performers("alpaca", _SID, idempotency_key="resume-3")["resume"](
            "desk-operator", None
        )

    assert exc.value.reason_code == "TERMINAL_EVIDENCE_UNREADABLE"
    assert exc.value.detail == decision.explanation


async def test_live_panel_skips_resume_admission_reconciliation(monkeypatch) -> None:
    calls = 0

    class _Registry:
        async def preview_resume_admission(self, _broker: str, _sid: str):
            nonlocal calls
            calls += 1
            raise AssertionError("live panel must not preview Resume")

        def dry_run_activity(self, _broker: str, _sid: str):
            return []

        def binding_for_control(self, _broker: str, _sid: str):
            return SimpleNamespace(symbol="SPY", use_rth=True)

    async def _account(*_args) -> str:
        return "account-1"

    async def _clerk(**_kwargs):
        return SimpleNamespace()

    async def _evidence(*_args, **_kwargs):
        return SimpleNamespace(
            status=status,
            projection=SimpleNamespace(),
            economics=SimpleNamespace(
                session_fills=(),
                snapshot=SimpleNamespace(
                    exposure={},
                    fills_today=0,
                    realized_pnl_today=0.0,
                    open_pnl=None,
                    last_activity_at_ms=None,
                ),
            ),
        )

    status = SimpleNamespace(running=True)
    sentinel = SimpleNamespace()
    monkeypatch.setattr(panel_data_source, "_validate_account", _account)
    monkeypatch.setattr(panel_data_source, "get_bot_task_registry", lambda: _Registry())
    monkeypatch.setattr(panel_data_source, "read_sqlite_panel_evidence", _evidence)
    monkeypatch.setattr(panel_data_source, "_clerk_status", _clerk)
    monkeypatch.setattr(panel_data_source, "read_sqlite_decision_receipts", lambda *_args: [])
    monkeypatch.setattr(panel_data_source, "panel_profile_for", lambda _broker: None)
    monkeypatch.setattr(panel_data_source, "build_market_pulse", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(panel_data_source, "build_panel", lambda *_args, **_kwargs: sentinel)
    monkeypatch.setattr(panel_data_source, "adapt_sqlite_panel", lambda panel, *_args, **_kwargs: panel)

    panel = await panel_data_source.get_panel("alpaca", "account-1", _SID)

    assert panel is sentinel
    assert calls == 0


async def test_pause_and_continue_performers_preserve_run_identity(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []

    class _Registry:
        async def pause(self, broker: str, sid: str, **_kwargs):
            calls.append(("pause", broker, sid))
            return SimpleNamespace(active_run_id="run-live")

        async def continue_paused(self, broker: str, sid: str, **_kwargs):
            calls.append(("continue", broker, sid))
            return SimpleNamespace(active_run_id="run-live")

    monkeypatch.setattr(
        "app.services.broker_v2_panel.panel_data_source.get_bot_task_registry",
        lambda: _Registry(),
    )
    performers = _action_performers("alpaca", _SID, idempotency_key="same-run-1")

    paused = await performers["pause"]("desk-operator", None)
    continued = await performers["continue"]("desk-operator", None)

    assert calls == [
        ("pause", "alpaca", _SID),
        ("continue", "alpaca", _SID),
    ]
    assert paused == "Run run-live is paused; its process remains live."
    assert continued == "Run run-live continued without changing run identity."


async def test_flatten_stop_stops_strategy_before_unprovable_exit(monkeypatch) -> None:
    events: list[str] = []
    binding = SimpleNamespace(run_id="run-1", action_plan=object(), quantity=1)

    class _Registry:
        def binding_for_control(self, broker: str, sid: str):
            assert (broker, sid) == ("alpaca", _SID)
            return binding

        def status(self, broker: str, sid: str):
            assert (broker, sid) == ("alpaca", _SID)
            return SimpleNamespace(running=True)

        async def stop(self, broker: str, sid: str, *, reason: str) -> None:
            assert (broker, sid) == ("alpaca", _SID)
            events.append("stop")

    class _Clerk:
        async def execute_for_instance(self, **kwargs):
            assert kwargs["strategy_instance_id"] == _SID
            events.append("execute")
            return SimpleNamespace(state=EffectOperationState.UNPROVABLE)

    monkeypatch.setattr(
        "app.services.broker_v2_panel.panel_data_source.get_bot_task_registry",
        lambda: _Registry(),
    )
    monkeypatch.setattr(
        "app.services.broker_v2_panel.panel_data_source.get_alpaca_clerk",
        lambda: _Clerk(),
    )

    message = await _action_performers("alpaca", _SID, idempotency_key="flatten-1")["flatten_stop"](
        "desk-operator", None
    )

    assert events == ["stop", "execute"]
    assert "cannot prove" in message


async def test_reconcile_performer_ignores_operator_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``reconcile_now`` has no operator-authored reason to journal; it ignores one."""

    class _Clerk:
        async def reconcile_once(self) -> str:
            return "clean"

    monkeypatch.setattr(
        "app.services.broker_v2_panel.panel_data_source.get_alpaca_clerk",
        lambda: _Clerk(),
    )

    message = await _action_performers(
        "alpaca", _SID, idempotency_key="reconcile-1"
    )["reconcile_now"]("desk-operator", "this should be ignored")

    assert message == "Reconciliation sweep complete: clean."


async def _noop() -> str:
    return "noop"


async def test_stale_revision_releases_key_for_corrected_retry() -> None:
    """A stale-revision rejection must not strand the idempotency key.

    The action never ran, so the key must be reusable: the pre-execution
    rejection frees the reservation instead of leaving it ``in_flight``, and a
    corrected retry (with the current revision) executes.
    """
    calls = 0

    async def _perform(operator: str, reason: str | None) -> str:
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
            performers={"stop": lambda op, reason: _noop()},  # retire not wired
            operator_identity="op",
            store=store,
        )

    assert (_SID, "retire", "na") not in store._records


async def test_duplicate_concurrent_posts_run_mutation_once() -> None:
    """Two concurrent POSTs with the same idempotency_key must fire the mutation once."""
    calls = 0

    async def _slow_perform(operator: str, reason: str | None) -> str:
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
    # One result is applied=True and one is the safe idempotent replay.
    assert applied_count + noop_count == 2


async def test_timed_out_duplicate_never_refires_in_flight_mutation() -> None:
    """A slow first command remains the sole owner after a duplicate times out."""
    calls = 0
    performer_started = asyncio.Event()
    release_performer = asyncio.Event()

    async def _blocked_perform(operator: str, reason: str | None) -> str:
        nonlocal calls
        calls += 1
        performer_started.set()
        await release_performer.wait()
        return "stopped"

    store = IdempotencyStore(wait_timeout_s=0.01)
    first_post = asyncio.create_task(
        execute_action(
            _request(key="slow-race"),
            sid=_SID,
            current_revision=42,
            current_concurrency_token="token",
            performers={"stop": _blocked_perform},
            operator_identity="op",
            store=store,
        )
    )
    await performer_started.wait()

    with pytest.raises(ActionOutcomeUnknownError, match="still processing"):
        await execute_action(
            _request(key="slow-race"),
            sid=_SID,
            current_revision=42,
            current_concurrency_token="token",
            performers={"stop": _blocked_perform},
            operator_identity="op",
            store=store,
        )

    assert calls == 1
    assert store._records[(_SID, "stop", "slow-race")].state == "in_flight"

    release_performer.set()
    result = await first_post
    assert result.applied is True
    assert calls == 1


def test_reason_left_optional_for_non_comment_actions() -> None:
    request = _request(action_id="resume", reason=None)

    assert request.reason is None
