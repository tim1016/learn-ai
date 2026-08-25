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
    StaleRevisionError,
)
from app.services.broker_v2_panel.sqlite_panel_source import execute_sqlite_panel_action

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
