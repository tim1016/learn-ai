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
from typing import get_args

import pytest
from pydantic import ValidationError

from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.sqlite.repository import (
    ExecutionLeaseLost,
    ExecutionLeaseLostAfterBrokerIO,
    RepositoryPoisoned,
)
from app.broker.v2panel.vocabulary import RetiredActionId
from app.schemas.broker_v2_panel import PanelActionRequest, PanelActionResult, PanelQuiesceActionRequest
from app.services.broker_v2_panel import panel_data_source
from app.services.broker_v2_panel.action_execution_service import (
    ActionNotAvailableError,
    ActionOutcomeUnknownError,
    ActivationFailedError,
    DurableIdempotencyStore,
    IdempotencyStore,
    StaleRevisionError,
    UnknownActionError,
    durable_idempotency_store_for,
    execute_action,
)
from app.services.broker_v2_panel.panel_data_source import run_action

_SID = "bot-alpha"


def _request(
    *,
    action_id: str = "archive",
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
        return "archived"

    store = IdempotencyStore()
    result = await execute_action(
        _request(),
        sid=_SID,
        current_revision=42,
        current_concurrency_token="token",
        performers={"archive": _perform},
        operator_identity="desk-operator",
        store=store,
    )

    assert result.applied is True
    assert result.outcome == "success"
    assert result.receipt_id == "k1"
    assert result.recorded_at_ms > 0
    assert result.action_id == "archive"
    assert result.message == "archived"
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
        return "archived"

    store = IdempotencyStore()
    await execute_action(
        _request(key="reason-thread", reason="operator note"),
        sid=_SID,
        current_revision=42,
        current_concurrency_token="token",
        performers={"archive": _perform},
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
            performers={"archive": _perform},
            operator_identity="op",
            store=IdempotencyStore(),
        )
    assert exc.value.http_status == 409


async def test_idempotent_repost_is_a_noop() -> None:
    calls = 0

    async def _perform(operator: str, reason: str | None) -> str:
        nonlocal calls
        calls += 1
        return "archived"

    store = IdempotencyStore()
    first = await execute_action(
        _request(key="dup"),
        sid=_SID,
        current_revision=42,
        current_concurrency_token="token",
        performers={"archive": _perform},
        operator_identity="op",
        store=store,
    )
    second = await execute_action(
        _request(key="dup"),
        sid=_SID,
        current_revision=42,
        current_concurrency_token="token",
        performers={"archive": _perform},
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
            performers={"archive": _perform},
            operator_identity="op",
            store=store,
        )

    assert "Inspect Clerk evidence" in (exc.value.detail or "")
    assert store._records[(_SID, "archive", "unknown")].state == "failed"


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
            performers={"archive": _perform},
            operator_identity="op",
            store=store,
        )

    assert store._records[(_SID, "archive", "mid-flight")].state == "failed"
    assert "panel_action_rejected" not in caplog.text


async def test_execution_lease_lost_propagates_unwrapped_for_the_adr0050_revival_catch() -> None:
    """B5: Archive dispatches through this shared executor, not
    ``sqlite_panel_source.execute_sqlite_panel_action`` (which returns
    ``None`` for ``SQLITE_PANEL_LIFECYCLE_ACTION_IDS`` and defers here).
    That module already lets ``ExecutionLeaseLost``/``RepositoryPoisoned``
    propagate past its own exception handling so
    ``panel_data_source.run_action``'s ADR 0050 revival catch can see them.
    Before this fix, this executor's blanket ``except Exception`` wrapped
    both into ``ActionOutcomeUnknownError`` (an opaque 500) before
    ``run_action`` ever got a chance to attempt a revival -- silently
    denying it the same self-cure every other action gets.
    """

    async def _perform(_operator: str, _reason: str | None) -> str:
        raise ExecutionLeaseLost("account lease lost mid-performer")

    store = IdempotencyStore()
    with pytest.raises(ExecutionLeaseLost):
        await execute_action(
            _request(action_id="archive", key="lease-lost"),
            sid=_SID,
            current_revision=42,
            current_concurrency_token="token",
            performers={"archive": _perform},
            operator_identity="op",
            store=store,
        )

    # Released, not burned failed (#1955 final review): every repository
    # mutation renews the execution lease as its very first statement under
    # the write lock, so a lost lease means nothing else ran here -- the same
    # "nothing applied" contract a pre-execution rejection gets. A cohort leg
    # the write path later revives needs this ORIGINAL key free for the
    # operator's same-key re-POST; burning it here left a revived leg
    # permanently unflattenable under its own key.
    assert (_SID, "archive", "lease-lost") not in store._records


async def test_a_lease_lost_after_broker_io_is_outcome_unknown_with_a_burned_key() -> None:
    """The release-not-burn contract above is only honest for a lease lost
    before any mutation. ``ClaimedBrokerIO`` renews after a broker call too,
    and a loss found there means an order may already be placed or cancelled
    with no receipt folded: outcome unknown, key burned, no retry offered --
    never "nothing applied, retry".
    """

    async def _perform(_operator: str, _reason: str | None) -> str:
        raise ExecutionLeaseLostAfterBrokerIO("lease lost after the broker acted", account_id="acct")

    store = IdempotencyStore()
    with pytest.raises(ActionOutcomeUnknownError) as unknown:
        await execute_action(
            _request(action_id="archive", key="lost-after-io"),
            sid=_SID,
            current_revision=42,
            current_concurrency_token="token",
            performers={"archive": _perform},
            operator_identity="op",
            store=store,
        )

    assert store._records[(_SID, "archive", "lost-after-io")].state == "failed"
    assert "may have been placed or cancelled" in (unknown.value.detail or "")


async def test_repository_poisoned_propagates_unwrapped_too() -> None:
    """The other half of B5's fix: ``RepositoryPoisoned`` gets the identical
    unwrapped treatment, so ``run_action``'s ``AuthorityPoisonedError``
    translation (distinct cure from a lost lease) also reaches Resume/
    Retire/Archive."""

    async def _perform(_operator: str, _reason: str | None) -> str:
        raise RepositoryPoisoned("account repository is poisoned")

    store = IdempotencyStore()
    with pytest.raises(RepositoryPoisoned):
        await execute_action(
            _request(action_id="archive", key="poisoned"),
            sid=_SID,
            current_revision=42,
            current_concurrency_token="token",
            performers={"archive": _perform},
            operator_identity="op",
            store=store,
        )

    assert store._records[(_SID, "archive", "poisoned")].state == "failed"


async def test_activation_failed_cleanup_proven_burns_the_key_as_a_known_failure() -> None:
    """PRD #1716 FR-6: cleanup-proven activation failures burn the
    idempotency key (post-execution) and report as-is, not wrapped into
    ActionOutcomeUnknownError."""

    async def _perform(_operator: str, _reason: str | None) -> str:
        raise ActivationFailedError(
            "Resume for run 'run-2' failed after Clerk registration; the Clerk stop committed.",
            detail="boom",
        )

    store = IdempotencyStore()
    with pytest.raises(ActivationFailedError) as exc:
        await execute_action(
            _request(action_id="reconcile_now", key="cleanup-proven-1"),
            sid=_SID,
            current_revision=42,
            current_concurrency_token="token",
            performers={"reconcile_now": _perform},
            operator_identity="op",
            store=store,
        )

    assert "the Clerk stop committed" in str(exc.value)
    assert store._records[(_SID, "reconcile_now", "cleanup-proven-1")].state == "failed"


async def test_activation_failed_replay_returns_the_same_failure_without_reexecuting() -> None:
    """PRD #1716 FR-6: reusing the same key replays the identical terminal
    failure without another activation attempt."""
    calls: list[str] = []

    async def _perform(_operator: str, _reason: str | None) -> str:
        calls.append("attempted")
        raise ActivationFailedError(
            "Resume for run 'run-2' failed after Clerk registration; the Clerk stop committed.",
        )

    store = IdempotencyStore()
    request = _request(action_id="reconcile_now", key="cleanup-proven-2")
    with pytest.raises(ActivationFailedError):
        await execute_action(
            request,
            sid=_SID,
            current_revision=42,
            current_concurrency_token="token",
            performers={"reconcile_now": _perform},
            operator_identity="op",
            store=store,
        )

    with pytest.raises(ActionNotAvailableError) as replay:
        await execute_action(
            request,
            sid=_SID,
            current_revision=42,
            current_concurrency_token="token",
            performers={"reconcile_now": _perform},
            operator_identity="op",
            store=store,
        )

    assert calls == ["attempted"]
    assert "previously failed" in str(replay.value)


async def test_disabled_presented_action_cannot_bypass_guard_via_post(
    monkeypatch,
    tmp_path: Path,
) -> None:
    async def _panel(*_args, **_kwargs):
        return SimpleNamespace(
            revision=7,
            actions=[
                SimpleNamespace(
                    action_id="archive",
                    label="Clear",
                    enabled=False,
                    blockers=[SimpleNamespace(
                        headline="Stop the bot before clearing it.",
                        detail="A running bot still evaluates bars and can place orders.",
                        condition=SimpleNamespace(id="BOT_STILL_RUNNING"),
                    )],
                    concurrency_token="token",
                )
            ],
        )

    async def _validated(*_args, **_kwargs) -> str:
        return "account-1"

    async def _panel_from_authority(*_args, **_kwargs):
        return await _panel(), [], None

    monkeypatch.setattr(
        "app.services.broker_v2_panel.panel_data_source._get_panel_with_entries_from_authority", _panel_from_authority,
    )
    monkeypatch.setattr("app.services.broker_v2_panel.panel_data_source.validate_account", _validated)
    monkeypatch.setattr(
        "app.services.broker_v2_panel.panel_data_source.get_bot_task_registry",
        lambda: SimpleNamespace(
            artifacts_root=tmp_path,
            binding_for_control=lambda *_: SimpleNamespace(mode="trade", broker="alpaca"),
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

    # The refusal is the guard's own: its headline, its why and its code.
    assert str(exc.value) == "Stop the bot before clearing it."
    assert exc.value.detail == "A running bot still evaluates bars and can place orders."
    assert exc.value.reason_code == "BOT_STILL_RUNNING"




async def test_idempotent_repost_survives_revision_advance() -> None:
    """A retry of an applied action stays a no-op even after the panel advances."""

    async def _perform(operator: str, reason: str | None) -> str:
        return "archived"

    store = IdempotencyStore()
    await execute_action(
        _request(key="dup", revision=42),
        sid=_SID,
        current_revision=42,
        current_concurrency_token="token",
        performers={"archive": _perform},
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
        performers={"archive": _perform},
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
        performers={"archive": lambda _operator, _reason: _noop()},
        operator_identity="op",
        store=IdempotencyStore(),
    )
    assert result.applied is True


async def test_durable_receipt_prevents_reexecution_after_store_restart(tmp_path: Path) -> None:
    calls = 0

    async def _perform(_operator: str, _reason: str | None) -> str:
        nonlocal calls
        calls += 1
        return "archived"

    path = tmp_path / "panel_action_receipts.json"
    first = await execute_action(
        _request(key="durable"),
        sid=_SID,
        current_revision=42,
        current_concurrency_token="token",
        performers={"archive": _perform},
        operator_identity="op",
        store=DurableIdempotencyStore(path),
    )
    replay = await execute_action(
        _request(key="durable"),
        sid=_SID,
        current_revision=99,
        current_concurrency_token="token",
        performers={"archive": _perform},
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
    compound = "\u001f".join((_SID, "archive", "legacy"))
    path.write_text(
        json.dumps(
            {
                compound: {
                    "state": "succeeded",
                    "result": {
                        "action_id": "archive",
                        "applied": True,
                        "revision": 42,
                        "concurrency_token": "token",
                        "message": "archived",
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
        performers={"archive": lambda _operator, _reason: _noop()},
        operator_identity="op",
        store=DurableIdempotencyStore(path),
    )

    assert result.applied is False
    assert result.receipt_id == "legacy"
    assert result.recorded_at_ms == legacy_observed_at_ms


@pytest.mark.parametrize("retired_action_id", get_args(RetiredActionId))
async def test_retired_action_receipts_stay_history_and_never_block_the_next_command(
    tmp_path: Path, retired_action_id: str,
) -> None:
    """#2550 review: a bot's ledger written before Resume/Pause/Continue (and,
    since #2578, Retire; since #2595, Flatten & stop; since #2605, the generic
    Stop; since #2635, Deploy and Cancel order) were retired must still load.
    Those receipts remain readable history, and the bot's next command runs
    instead of failing the load."""
    path = tmp_path / "panel_action_receipts.json"
    compound = "\u001f".join((_SID, retired_action_id, "before-retirement"))
    history = {
        compound: {
            "state": "succeeded",
            "result": {
                "action_id": retired_action_id,
                "outcome": "success",
                "receipt_id": "before-retirement",
                "recorded_at_ms": 1_700_000_000_000,
                "applied": True,
                "revision": 7,
                "concurrency_token": "token",
                "message": f"{retired_action_id} applied",
            },
            "error_detail": None,
        }
    }
    path.write_text(json.dumps(history), encoding="utf-8")

    result = await execute_action(
        _request(key="after-retirement"),
        sid=_SID,
        current_revision=42,
        current_concurrency_token="token",
        performers={"archive": lambda _operator, _reason: _noop()},
        operator_identity="op",
        store=DurableIdempotencyStore(path),
    )

    assert result.applied is True
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted[compound] == history[compound]
    assert len(persisted) == 2


@pytest.mark.parametrize("retired_action_id", get_args(RetiredActionId))
@pytest.mark.parametrize("model", [PanelActionRequest, PanelQuiesceActionRequest])
def test_a_retired_action_id_is_refused_at_the_request_schema(
    model: type[PanelActionRequest], retired_action_id: str,
) -> None:
    """A retired id -- the generic Stop included (#2605) -- is history only: no
    request may name it, so it is refused before any performer is reached."""
    with pytest.raises(ValidationError):
        model(action_id=retired_action_id, revision=1, concurrency_token="t", idempotency_key="k")  # type: ignore[arg-type]


async def test_an_unreadable_ledger_refuses_every_command_and_keeps_its_file(tmp_path: Path) -> None:
    """A load failure must not mark the ledger loaded: the next command would
    start from an empty ledger and overwrite the bot's receipt history."""
    path = tmp_path / "panel_action_receipts.json"
    compound = "\u001f".join((_SID, "archive", "truncated"))
    path.write_text(json.dumps({compound: {"state": "succeeded", "result": {"action_id": "archive"}}}), encoding="utf-8")
    original = path.read_text(encoding="utf-8")
    store = DurableIdempotencyStore(path)

    for _ in range(2):
        with pytest.raises(ValidationError):
            await store.reserve_or_get(_SID, "archive", "next-command")

    assert path.read_text(encoding="utf-8") == original


async def test_durable_store_keeps_receipts_beside_the_instance_artifacts(tmp_path: Path) -> None:
    """The ledger stays at ``live_state/<sid>/panel_action_receipts.json``, the
    location every existing bot's receipts were written to, so an upgrade still
    replays them instead of re-firing a completed command."""
    store = durable_idempotency_store_for(tmp_path, _SID)

    assert await store.reserve_or_get(_SID, "archive", "first") is None

    assert (tmp_path / "live_state" / _SID / "panel_action_receipts.json").is_file()


@pytest.mark.parametrize("hostile_sid", ["../escape", "evil id", "a/b", ""])
def test_durable_store_refuses_an_id_that_is_not_one_confined_segment(
    tmp_path: Path, hostile_sid: str,
) -> None:
    """The store builds its own path from the request's id through the confined
    per-instance directory, so no caller -- production registry or test double
    -- can hand it a file outside the artifacts root (CodeQL py/path-injection,
    #2554)."""
    artifacts_root = tmp_path / "artifacts"

    with pytest.raises(ValueError):
        durable_idempotency_store_for(artifacts_root, hostile_sid)

    assert not artifacts_root.exists()
    assert not (tmp_path / "escape").exists()


async def test_unwired_action_is_typed_not_available() -> None:
    with pytest.raises(ActionNotAvailableError) as exc:
        await execute_action(
            _request(action_id="open_custody_timeline"),
            sid=_SID,
            current_revision=42,
            current_concurrency_token="token",
            performers={"archive": lambda op, reason: _noop()},  # open_custody_timeline not wired
            operator_identity="op",
            store=IdempotencyStore(),
        )
    assert exc.value.http_status == 409










async def test_live_panel_skips_resume_admission_reconciliation(monkeypatch) -> None:
    calls = 0

    class _Registry:
        async def preview_resume_admission(self, _broker: str, _sid: str):
            nonlocal calls
            calls += 1
            raise AssertionError("live panel must not preview Resume")

        def dry_run_activity(self, _broker: str, _sid: str):
            return []

        def bot_end(self, _broker: str, _sid: str):
            return None

        def binding_for_control(self, _broker: str, sid: str):
            return SimpleNamespace(
                strategy_instance_id=sid,
                run_id="run-1",
                symbol="SPY",
                use_rth=True,
                mode="trade",
                strategy_key="deployment_validation",
                sealed_program=None,
            )

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
                    exact_open_pnl=None,
                    last_activity_at_ms=None,
                ),
            ),
        )

    status = SimpleNamespace(running=True)
    sentinel = SimpleNamespace()
    monkeypatch.setattr(panel_data_source, "validate_account", _account)
    monkeypatch.setattr(panel_data_source, "get_bot_task_registry", lambda: _Registry())
    monkeypatch.setattr(panel_data_source, "custody_facade", lambda _runtime: SimpleNamespace(
        account_id="account-1", repository=None, program_leg_policy=ProgramLegPolicy.regular_only(),
        flatten_send_verdict=lambda: None,
    ))
    monkeypatch.setattr(panel_data_source, "read_sqlite_panel_evidence", _evidence)
    monkeypatch.setattr(panel_data_source, "clerk_status", _clerk)
    monkeypatch.setattr(
        panel_data_source,
        "read_sqlite_decision_receipts",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(panel_data_source, "build_market_pulse", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(panel_data_source, "custody_bot_status", lambda *_args, **_kwargs: "running")
    monkeypatch.setattr(panel_data_source, "build_panel", lambda *_args, **_kwargs: sentinel)
    monkeypatch.setattr(panel_data_source, "adapt_sqlite_panel", lambda panel, *_args, **_kwargs: panel)

    panel = await panel_data_source.get_panel("alpaca", "account-1", _SID)

    assert panel is sentinel
    assert calls == 0


def _clock_evidence(observed_at_ms: int):
    from app.broker.contract.models import BrokerClockEvidence

    return BrokerClockEvidence(
        broker="alpaca", is_open=True, vendor_timestamp_ms=observed_at_ms,
        next_open_ms=None, next_close_ms=None, observed_at_ms=observed_at_ms,
    )


def _ibkr_snapshot(observed_at_ms: int):
    from app.schemas.market_liveness import (
        MarketStatusSnapshot,
        MarketStatusSource,
        SymbolMarketDataEvidence,
        SymbolTradingStatusEvidence,
    )

    return MarketStatusSnapshot(
        source=MarketStatusSource.IBKR, connected=True, observed_at_ms=observed_at_ms,
        connection_changed_at_ms=observed_at_ms - 60_000,
        symbol_statuses=(SymbolTradingStatusEvidence(
            symbol="SPY", state="TRADABLE", source=MarketStatusSource.IBKR,
            observed_at_ms=observed_at_ms - 60_000,
        ),),
        subscriptions=(SymbolMarketDataEvidence(
            symbol="SPY", generation="gen-1", state="READY", observed_at_ms=observed_at_ms,
            valid_until_ms=observed_at_ms + 5_000, last_received_at_ms=observed_at_ms,
            quote_received_at_ms=observed_at_ms, reason_code="MARKET_DATA_READY", reason="ready",
        ),),
    )


@pytest.mark.parametrize("source", ["alpaca_clock", "ibkr_market_data"])
async def test_panel_liveness_is_evaluated_after_evidence_lands_mid_request(monkeypatch, source: str) -> None:
    """#2256 / #2257: the broker-clock poller (1 s) and the IBKR tick
    publisher (every callback) re-stamp their evidence while the panel awaits
    its own reads. Liveness evaluated at the request-start instant then saw
    evidence "from the future" and reported MARKET_CLOCK_INVALID or
    MARKET_DATA_RECOVERING on a healthy feed; it must be evaluated at an
    instant captured after those awaits."""
    from app.schemas.market_liveness import MarketStatusSource
    from app.services.market_liveness import (
        get_market_liveness_store,
        market_liveness_fact,
        reset_market_liveness_store_for_testing,
    )

    request_start_ms = 1_700_000_000_000
    wall = {"now": request_start_ms}

    def _land(observed_at_ms: int) -> None:
        store = get_market_liveness_store()
        if source == "alpaca_clock":
            store.observe_clock(_clock_evidence(observed_at_ms))
        else:
            store.apply_status_snapshot(_ibkr_snapshot(observed_at_ms), now_ms=observed_at_ms)

    class _Registry:
        def dry_run_activity(self, _broker: str, _sid: str):
            return []

        def bot_end(self, _broker: str, _sid: str):
            return None

        def binding_for_control(self, _broker: str, sid: str):
            return SimpleNamespace(
                strategy_instance_id=sid, run_id="run-1", symbol="SPY", use_rth=True,
                mode="trade", strategy_key="deployment_validation", sealed_program=None,
            )

    async def _account(*_args) -> str:
        return "account-1"

    async def _clerk(**_kwargs):
        # Fresh evidence lands during this await, as it does on a live feed.
        wall["now"] = request_start_ms + 40
        _land(wall["now"])
        wall["now"] += 5
        return SimpleNamespace()

    async def _evidence(*_args, **_kwargs):
        return SimpleNamespace(
            status=SimpleNamespace(running=True),
            projection=SimpleNamespace(),
            economics=SimpleNamespace(
                session_fills=(),
                snapshot=SimpleNamespace(
                    exposure={}, fills_today=0, realized_pnl_today=0.0,
                    exact_open_pnl=None, last_activity_at_ms=None,
                ),
            ),
        )

    evaluated: list[object] = []

    def _market_pulse(*_args, now_ms: int, symbol: str, liveness=None, **_kwargs):
        # build_market_pulse's own default when the caller supplies no fact.
        evaluated.append(liveness or market_liveness_fact(symbol, now_ms))
        return SimpleNamespace()

    reset_market_liveness_store_for_testing()
    store = get_market_liveness_store()
    if source == "alpaca_clock":
        store.mark_stream_connected(observed_at_ms=request_start_ms - 1_000)
    else:
        store.require_source(MarketStatusSource.IBKR)
        store.observe_clock(_clock_evidence(request_start_ms - 100))
        _land(request_start_ms - 100)
    monkeypatch.setattr(panel_data_source, "now_ms_utc", lambda: wall["now"])
    monkeypatch.setattr(panel_data_source, "validate_account", _account)
    monkeypatch.setattr(panel_data_source, "get_bot_task_registry", lambda: _Registry())
    monkeypatch.setattr(panel_data_source, "custody_facade", lambda _runtime: SimpleNamespace(
        account_id="account-1", repository=None, program_leg_policy=ProgramLegPolicy.regular_only(),
        flatten_send_verdict=lambda: None,
    ))
    monkeypatch.setattr(panel_data_source, "read_sqlite_panel_evidence", _evidence)
    monkeypatch.setattr(panel_data_source, "clerk_status", _clerk)
    monkeypatch.setattr(panel_data_source, "read_sqlite_decision_receipts", lambda *_a, **_k: [])
    monkeypatch.setattr(panel_data_source, "build_market_pulse", _market_pulse)
    monkeypatch.setattr(panel_data_source, "custody_bot_status", lambda *_args, **_kwargs: "running")
    monkeypatch.setattr(panel_data_source, "build_panel", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(panel_data_source, "adapt_sqlite_panel", lambda panel, *_args, **_kwargs: panel)

    try:
        await panel_data_source.get_panel("alpaca", "account-1", _SID)
    finally:
        reset_market_liveness_store_for_testing()

    [liveness] = evaluated
    assert (liveness.state, liveness.reason_code) == ("TRADABLE", "MARKET_TRADABLE")


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
        return "archived"

    store = IdempotencyStore()

    with pytest.raises(StaleRevisionError):
        await execute_action(
            _request(key="retry", token="stale-token"),
            sid=_SID,
            current_revision=42,
            current_concurrency_token="token",
            performers={"archive": _perform},
            operator_identity="op",
            store=store,
        )

    # Nothing was applied → no dangling in_flight reservation for the key.
    assert (_SID, "archive", "retry") not in store._records

    result = await execute_action(
        _request(key="retry", revision=42),
        sid=_SID,
        current_revision=42,
        current_concurrency_token="token",
        performers={"archive": _perform},
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
            _request(action_id="open_custody_timeline", key="na"),
            sid=_SID,
            current_revision=42,
            current_concurrency_token="token",
            performers={"archive": lambda op, reason: _noop()},  # open_custody_timeline not wired
            operator_identity="op",
            store=store,
        )

    assert (_SID, "open_custody_timeline", "na") not in store._records


async def test_duplicate_concurrent_posts_run_mutation_once() -> None:
    """Two concurrent POSTs with the same idempotency_key must fire the mutation once."""
    calls = 0

    async def _slow_perform(operator: str, reason: str | None) -> str:
        nonlocal calls
        calls += 1
        # Yield briefly so the second coroutine can reach reserve_or_get before we finish.
        await asyncio.sleep(0)
        return "archived"

    store = IdempotencyStore()

    async def _post() -> PanelActionResult | None:
        try:
            return await execute_action(
                _request(key="race"),
                sid=_SID,
                current_revision=42,
                current_concurrency_token="token",
                performers={"archive": _slow_perform},
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
        return "archived"

    store = IdempotencyStore(wait_timeout_s=0.01)
    first_post = asyncio.create_task(
        execute_action(
            _request(key="slow-race"),
            sid=_SID,
            current_revision=42,
            current_concurrency_token="token",
            performers={"archive": _blocked_perform},
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
            performers={"archive": _blocked_perform},
            operator_identity="op",
            store=store,
        )

    assert calls == 1
    assert store._records[(_SID, "archive", "slow-race")].state == "in_flight"

    release_performer.set()
    result = await first_post
    assert result.applied is True
    assert calls == 1


def test_reason_left_optional_for_non_comment_actions() -> None:
    request = _request(action_id="reconcile_now", reason=None)

    assert request.reason is None


def test_program_build_for_display_prefers_frozen_evidence_over_live_check(monkeypatch) -> None:
    """#1728 Gap 2: the panel must show what was proven and recorded for the
    current run, not a fresh re-check that can drift from what is actually
    running underfoot."""
    from app.services.bot_binding_repository import ProgramBuildRunEvidence

    evidence = ProgramBuildRunEvidence(
        strategy_instance_id=_SID,
        run_id="run-1",
        sealed_program_hash="a" * 64,
        program_version="ema-crossover-signal/v1",
        golden_trace_root="b" * 64,
        running_artifact_digest="c" * 64,
        qualification_receipt_hash="d" * 64,
        verified_at_ms=1_000,
    )

    class _FakeRepo:
        def read_program_build_evidence(self, strategy_instance_id: str, run_id: str):
            assert (strategy_instance_id, run_id) == (_SID, "run-1")
            return evidence

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("must not fall back to a live re-check when frozen evidence exists")

    monkeypatch.setattr(panel_data_source, "_run_evidence_repository", lambda: _FakeRepo())
    monkeypatch.setattr(panel_data_source, "prove_running_program_build", _must_not_run)

    binding = SimpleNamespace(
        strategy_instance_id=_SID, run_id="run-1", strategy_key="ema_crossover_signal"
    )

    fact = panel_data_source._program_build_for_display(binding, verified_at_ms=9_999)

    assert fact.state == "PROVEN"
    assert fact.program_key == "ema_crossover_signal"
    assert fact.program_version == evidence.program_version
    assert fact.golden_trace_root == evidence.golden_trace_root
    assert fact.running_artifact_digest == evidence.running_artifact_digest
    assert fact.qualification_receipt_hash == evidence.qualification_receipt_hash
    # The frozen record's own verified_at_ms is replayed, never today's clock.
    assert fact.verified_at_ms == 1_000


def test_program_build_for_display_falls_back_to_live_check_when_no_evidence_recorded(
    monkeypatch,
) -> None:
    """No durable per-run record exists (never PROVEN at Start, or a run that
    predates #1728) — the panel must fall back to the canonical live proof
    rather than fabricate a frozen verdict."""

    class _FakeRepo:
        def read_program_build_evidence(self, _strategy_instance_id: str, _run_id: str):
            return None

    sentinel = SimpleNamespace()

    def _live_proof(binding, *, verified_at_ms):
        assert binding.strategy_instance_id == _SID
        assert verified_at_ms == 9_999
        return sentinel

    monkeypatch.setattr(panel_data_source, "_run_evidence_repository", lambda: _FakeRepo())
    monkeypatch.setattr(panel_data_source, "prove_running_program_build", _live_proof)

    binding = SimpleNamespace(
        strategy_instance_id=_SID, run_id="run-1", strategy_key="deployment_validation"
    )

    fact = panel_data_source._program_build_for_display(binding, verified_at_ms=9_999)

    assert fact is sentinel
