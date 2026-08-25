"""Optimistic-concurrency fence of ``execute_sqlite_panel_action``.

Regression coverage for the 2026-08-25 fleet stress run
(docs/audits/bot-fleet-stress-2026-08-25.md, S16): the fence previously
required strict ``request.revision == panel.revision``, but every panel read
bumps the projection revision — including the executor's own re-derivation
during validation — so the fence could never pass. The action-scoped
``concurrency_token`` is the authoritative staleness check per the
``PanelAction`` contract and is what the shared executor already validates.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.schemas.broker_v2_panel import PanelActionRequest
from app.services.broker_v2_panel.action_execution_service import (
    ActionNotAvailableError,
    IdempotencyStore,
    StaleRevisionError,
)
from app.services.broker_v2_panel.sqlite_panel_source import (
    execute_sqlite_panel_action,
)

_SID = "bot-alpha"


def _request(*, action_id: str, revision: int = 42, token: str = "token") -> PanelActionRequest:
    return PanelActionRequest(
        action_id=action_id,  # type: ignore[arg-type]
        revision=revision,
        concurrency_token=token,
        idempotency_key="k1",
        reason=None,
    )


async def test_fence_ignores_revision_drift_when_token_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matching tokens carry the action past the fence despite revision drift."""
    monkeypatch.setattr(
        "app.services.broker_v2_panel.sqlite_panel_source.active_sqlite_facade",
        lambda _broker: object(),
    )

    with pytest.raises(ActionNotAvailableError):
        # prepare_safe_flatten is refused AFTER the fence (view action on the
        # SQLite adapter), so reaching ActionNotAvailableError instead of
        # StaleRevisionError proves the fence passed on matching tokens.
        await execute_sqlite_panel_action(
            "alpaca",
            "account-1",
            _SID,
            request=_request(action_id="prepare_safe_flatten", revision=1, token="token"),
            panel=SimpleNamespace(revision=99),
            action=SimpleNamespace(concurrency_token="token"),
            availability_error=None,
        )


async def test_fence_rejects_token_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuinely re-presented action (new concurrency token) must still 409."""
    monkeypatch.setattr(
        "app.services.broker_v2_panel.sqlite_panel_source.active_sqlite_facade",
        lambda _broker: object(),
    )

    with pytest.raises(StaleRevisionError) as exc:
        await execute_sqlite_panel_action(
            "alpaca",
            "account-1",
            _SID,
            request=_request(action_id="prepare_safe_flatten", token="stale-token"),
            panel=SimpleNamespace(revision=42),
            action=SimpleNamespace(concurrency_token="fresh-token"),
            availability_error=None,
        )
    assert exc.value.http_status == 409


async def test_context_read_failure_releases_the_same_key_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed pre-execution projection read leaves no in-flight command."""
    facade = SimpleNamespace(repository=object())
    store = IdempotencyStore(wait_timeout_s=0)
    monkeypatch.setattr(
        "app.services.broker_v2_panel.sqlite_panel_source.active_sqlite_facade",
        lambda _broker: facade,
    )
    monkeypatch.setattr(
        "app.services.broker_v2_panel.sqlite_panel_source.SqliteClerkProjectionReader.from_repository",
        lambda _repository: (_ for _ in ()).throw(RuntimeError("projection read failed")),
    )

    async def attempt() -> None:
        await execute_sqlite_panel_action(
            "alpaca",
            "account-1",
            _SID,
            request=_request(action_id="reconcile_now"),
            panel=SimpleNamespace(revision=42),
            action=SimpleNamespace(concurrency_token="token", label="Reconcile now"),
            availability_error=None,
            store=store,
        )

    with pytest.raises(RuntimeError, match="projection read failed"):
        await attempt()
    with pytest.raises(RuntimeError, match="projection read failed"):
        await attempt()


async def test_stop_failure_releases_the_same_key_to_redrive_quiescence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry reaches durable STOP replay after post-commit quiescence fails."""

    class _Reader:
        def recovery_context(self, *, strategy_instance_id: str) -> SimpleNamespace:
            assert strategy_instance_id == _SID
            return SimpleNamespace(strategy_instance_id=_SID)

        def close(self) -> None:
            return None

    attempts = 0
    facade = SimpleNamespace(repository=object())
    store = IdempotencyStore(wait_timeout_s=0)
    monkeypatch.setattr(
        "app.services.broker_v2_panel.sqlite_panel_source.active_sqlite_facade",
        lambda _broker: facade,
    )
    monkeypatch.setattr(
        "app.services.broker_v2_panel.sqlite_panel_source.SqliteClerkProjectionReader.from_repository",
        lambda _repository: _Reader(),
    )
    monkeypatch.setattr(
        "app.services.broker_v2_panel.sqlite_panel_source.build_recovery_catalog",
        lambda _context: [
            SimpleNamespace(action_id="stop_bot_decisions", execution_ref="run-1")
        ],
    )

    async def execute_stop(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("local quiescence failed after durable STOP")
        return SimpleNamespace(
            receipt_id="cmd:stop:run-1",
            recorded_at_ms=1_700_000_000_000,
            applied=False,
        )

    monkeypatch.setattr(
        "app.services.broker_v2_panel.sqlite_panel_source.execute_recovery_action",
        execute_stop,
    )
    request = _request(action_id="stop_bot_decisions")

    with pytest.raises(RuntimeError, match="local quiescence failed"):
        await execute_sqlite_panel_action(
            "alpaca",
            "account-1",
            _SID,
            request=request,
            panel=SimpleNamespace(revision=42),
            action=SimpleNamespace(concurrency_token="token", label="Stop decisions"),
            availability_error=None,
            store=store,
        )

    retried = await execute_sqlite_panel_action(
        "alpaca",
        "account-1",
        _SID,
        request=request,
        panel=SimpleNamespace(revision=43),
        action=SimpleNamespace(concurrency_token="token", label="Stop decisions"),
        availability_error=None,
        store=store,
    )

    assert retried is not None and retried.applied is False
    assert retried.receipt_id == "cmd:stop:run-1"
    assert attempts == 2
