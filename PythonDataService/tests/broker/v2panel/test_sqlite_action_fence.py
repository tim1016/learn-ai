"""Optimistic-concurrency fence of ``execute_sqlite_panel_action``.

Regression coverage for the 2026-08-25 fleet stress run
(S16): the fence previously
required strict ``request.revision == panel.revision``, but every panel read
bumps the projection revision — including the executor's own re-derivation
during validation — so the fence could never pass. The action-scoped
``concurrency_token`` is the authoritative staleness check per the
``PanelAction`` contract and is what the shared executor already validates.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.broker.alpaca.clerk.sqlite.repository import ExecutionLeaseLost, RepositoryPoisoned
from app.schemas.broker_v2_panel import PanelActionRequest
from app.services.broker_v2_panel.action_execution_service import (
    ActionNotAvailableError,
    ActionOutcomeUnknownError,
    IdempotencyStore,
    StaleRevisionError,
)
from app.services.broker_v2_panel.sqlite_panel_source import (
    SqlitePanelBotNotFound,
    execute_sqlite_panel_action,
)

_SID = "bot-alpha"


async def _no_published_pass(_strategy_instance_id: str) -> None:
    """The stand-in authority has published no account pass (#2839)."""
    return None


def _request(*, action_id: str, revision: int = 42, token: str = "token") -> PanelActionRequest:
    return PanelActionRequest(
        action_id=action_id,  # type: ignore[arg-type]
        revision=revision,
        concurrency_token=token,
        idempotency_key="k1",
        reason=None,
    )


async def test_fence_ignores_revision_drift_when_token_matches() -> None:
    """Matching tokens carry the action past the fence despite revision drift."""
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
            facade=object(),
        )


async def test_fence_rejects_token_mismatch() -> None:
    """A genuinely re-presented action (new concurrency token) must still 409."""
    with pytest.raises(StaleRevisionError) as exc:
        await execute_sqlite_panel_action(
            "alpaca",
            "account-1",
            _SID,
            request=_request(action_id="prepare_safe_flatten", token="stale-token"),
            panel=SimpleNamespace(revision=42),
            action=SimpleNamespace(concurrency_token="fresh-token"),
            availability_error=None,
            facade=object(),
        )
    assert exc.value.http_status == 409


async def test_context_read_failure_releases_the_same_key_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed pre-execution projection read leaves no in-flight command."""
    facade = SimpleNamespace(repository=object(), authority_kind="sqlite", published_pass=_no_published_pass)
    store = IdempotencyStore(wait_timeout_s=0)
    monkeypatch.setattr(
        "app.services.broker_v2_panel.sqlite_panel_source.SqliteClerkProjectionReader.from_facade",
        lambda _facade: (_ for _ in ()).throw(RuntimeError("projection read failed")),
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
            facade=facade,
            store=store,
        )

    with pytest.raises(RuntimeError, match="projection read failed"):
        await attempt()
    with pytest.raises(RuntimeError, match="projection read failed"):
        await attempt()


async def test_stop_failure_releases_the_same_key_to_redrive_quiescence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed post-commit quiescence releases the action's key, so the same key can drive it again.

    The fake catalog keeps offering ``execution_ref`` after the STOP; the real
    one offers none once no run is ACTIVE, so a panel retry is refused
    ``NO_ACTIVE_BOT_RUN`` rather than replayed. This pins only the release.
    """

    class _Reader:
        def recovery_context(self, *, strategy_instance_id: str, published_pass: None) -> SimpleNamespace:
            assert strategy_instance_id == _SID
            return SimpleNamespace(strategy_instance_id=_SID)

        def close(self) -> None:
            return None

    attempts = 0
    facade = SimpleNamespace(repository=object(), authority_kind="sqlite", published_pass=_no_published_pass)
    store = IdempotencyStore(wait_timeout_s=0)
    monkeypatch.setattr(
        "app.services.broker_v2_panel.sqlite_panel_source.SqliteClerkProjectionReader.from_facade",
        lambda _facade: _Reader(),
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

    with pytest.raises(ActionOutcomeUnknownError) as unknown:
        await execute_sqlite_panel_action(
            "alpaca",
            "account-1",
            _SID,
            request=request,
            panel=SimpleNamespace(revision=42),
            action=SimpleNamespace(concurrency_token="token", label="Stop decisions"),
            availability_error=None,
            facade=facade,
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
        facade=facade,
        store=store,
    )

    assert "Inspect Clerk evidence" in (unknown.value.detail or "")
    assert retried is not None and retried.applied is False
    assert retried.receipt_id == "cmd:stop:run-1"
    assert attempts == 2


def _performer_that_raises(monkeypatch: pytest.MonkeyPatch, error: Exception) -> SimpleNamespace:
    """A facade whose recovery catalog offers ``reconcile_now`` and whose performer raises ``error``."""

    class _Reader:
        def recovery_context(self, *, strategy_instance_id: str, published_pass: None) -> SimpleNamespace:
            return SimpleNamespace(strategy_instance_id=strategy_instance_id)

        def close(self) -> None:
            return None

    async def execute(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(
        "app.services.broker_v2_panel.sqlite_panel_source.SqliteClerkProjectionReader.from_facade",
        lambda _facade: _Reader(),
    )
    monkeypatch.setattr(
        "app.services.broker_v2_panel.sqlite_panel_source.build_recovery_catalog",
        lambda _context: [SimpleNamespace(action_id="reconcile_now", execution_ref=None)],
    )
    monkeypatch.setattr(
        "app.services.broker_v2_panel.sqlite_panel_source.execute_recovery_action", execute
    )
    return SimpleNamespace(repository=object(), authority_kind="sqlite", published_pass=_no_published_pass)


async def _post_reconcile(facade: SimpleNamespace, store: IdempotencyStore) -> object:
    return await execute_sqlite_panel_action(
        "alpaca",
        "account-1",
        _SID,
        request=_request(action_id="reconcile_now"),
        panel=SimpleNamespace(revision=42),
        action=SimpleNamespace(concurrency_token="token", label="Reconcile now"),
        availability_error=None,
        facade=facade,
        store=store,
    )


async def test_a_performer_failing_after_it_began_is_an_outcome_unknown_and_burns_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared executor answers any performer failure as a typed unknown outcome; so does this one.

    Raw, it reached the client as the global 500 with the exception's own
    text, and the key it burned said nothing about why it could not be reused.
    """
    facade = _performer_that_raises(monkeypatch, RuntimeError("broker connection dropped after dispatch"))
    store = IdempotencyStore(wait_timeout_s=0)

    with pytest.raises(ActionOutcomeUnknownError) as unknown:
        await _post_reconcile(facade, store)
    with pytest.raises(ActionNotAvailableError, match="previously failed"):
        await _post_reconcile(facade, store)

    assert "Inspect Clerk evidence" in (unknown.value.detail or "")
    assert "dropped after dispatch" not in str(unknown.value)


@pytest.mark.parametrize(
    ("error", "key_after"),
    [
        (ExecutionLeaseLost("lease lost before any write"), None),
        (RepositoryPoisoned("mirror finalize unconfirmed"), "failed"),
        (SqlitePanelBotNotFound.for_bot(_SID), "failed"),
    ],
    ids=["lease-lost-releases-the-key", "poisoned-burns-the-key", "bot-not-found-burns-the-key"],
)
async def test_the_conditions_run_action_answers_from_stay_raw(
    monkeypatch: pytest.MonkeyPatch, error: Exception, key_after: str | None,
) -> None:
    """``run_action`` revives a lost lease, names the poison and answers a vanished bot 404
    (``_run_action_under_live_authority``) only from the raw condition."""
    facade = _performer_that_raises(monkeypatch, error)
    store = IdempotencyStore(wait_timeout_s=0)

    with pytest.raises(type(error)):
        await _post_reconcile(facade, store)

    record = store._records.get((_SID, "reconcile_now", "k1"))
    assert (None if record is None else record.state) == key_after


async def test_a_replayed_burned_key_never_carries_the_performers_exception_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A burned key's reason is authored, not the exception's text (a database path, say, #1865).

    The first attempt answers a fixed sentence; so does every replay of its
    key. The exact text stays in the server's log, where an operator looks.
    """
    secret = "secret /var/db/path"
    facade = _performer_that_raises(monkeypatch, RuntimeError(secret))
    store = IdempotencyStore(wait_timeout_s=0)

    with caplog.at_level("ERROR"), pytest.raises(ActionOutcomeUnknownError) as first:
        await _post_reconcile(facade, store)
    with pytest.raises(ActionNotAvailableError) as replay:
        await _post_reconcile(facade, store)

    for refused in (first.value, replay.value):
        assert secret not in f"{refused} {refused.detail}"
    assert "outcome is unknown" in (replay.value.detail or "")
    assert secret in caplog.text
