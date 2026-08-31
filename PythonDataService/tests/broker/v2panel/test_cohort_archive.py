"""Cohort-scoped archive (ADR 0052, #1911): presentation + batch execution.

The batch *taxonomy* — derived per-leg identity, typed per-leg outcomes,
continue-past-refusal, account-scoped early exit — lives in the shared
executor and is pinned once by ``test_cohort_flatten``. Duplicating those
five cases here would test the same function twice, so what this file pins
is what archive actually adds: which members are candidates, that the
request cannot name an action other than archive, and that the presented
leg facts are the per-bot panel's real ones against the real harness.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from pydantic import ValidationError

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.routers.broker_v2_panel import router
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import (
    CohortArchiveLegRequest,
    CohortArchiveRequest,
    PanelActionRequest,
    PanelActionResult,
)
from app.services.bot_runner import set_bot_task_registry
from app.services.broker_v2_panel import cohort_archive, panel_data_source
from app.services.broker_v2_panel.action_execution_service import (
    reset_idempotency_store_for_testing,
)
from tests.broker.alpaca.clerk.sqlite.conftest import _FakeReadPort, _FakeTradePort
from tests.broker.v2panel.fixtures import ACCT
from tests.broker.v2panel.test_panel_router import _FakeBrokerPort, _FakeRegistry

_STOPPED_SIDS = ("spy-done-1", "spy-done-2")
_RUNNING_SID = "spy-live-1"


async def _accept_account(broker: str, account_id: str) -> str:
    return account_id


def _request(*sids: str) -> CohortArchiveRequest:
    return CohortArchiveRequest(
        idempotency_key="sweep-1",
        reason="finished with these",
        legs=[
            CohortArchiveLegRequest(
                strategy_instance_id=sid, revision=1, concurrency_token="token-1"
            )
            for sid in sids
        ],
    )


# ── request contract ─────────────────────────────────────────────────────────


def test_a_leg_cannot_name_the_action_it_executes() -> None:
    """The endpoint archives, and nothing else.

    A client able to name the action could reach a different mutation through
    a surface whose confirmation copy described archiving.
    """
    with pytest.raises(ValidationError):
        CohortArchiveLegRequest(
            strategy_instance_id="spy-done-1",
            revision=1,
            concurrency_token="token-1",
            action_id="flatten_stop",  # type: ignore[call-arg]
        )


def test_duplicate_legs_are_refused() -> None:
    with pytest.raises(ValueError, match="distinct bots"):
        _request("spy-done-1", "spy-done-1")


def test_the_derived_leg_identity_budget_is_enforced() -> None:
    with pytest.raises(ValueError, match="identity budget"):
        CohortArchiveRequest(
            idempotency_key="k" * 64,
            legs=[
                CohortArchiveLegRequest(
                    strategy_instance_id="s" * 96,
                    revision=1,
                    concurrency_token="t",
                )
            ],
        )


# ── service-level: the action archive legs actually run ──────────────────────


async def test_every_leg_runs_the_per_bot_archive_under_a_derived_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str, str]] = []

    async def fake_run_action(
        broker: str,
        account_id: str,
        sid: str,
        request: PanelActionRequest,
        *,
        operator_identity: str,
    ) -> PanelActionResult:
        seen.append((sid, request.action_id, request.idempotency_key))
        return PanelActionResult(
            action_id="archive",  # type: ignore[arg-type]
            receipt_id="receipt-1",
            recorded_at_ms=1,
            applied=True,
            revision=1,
            concurrency_token="token-1",
            message="archived",
        )

    monkeypatch.setattr(panel_data_source, "run_action", fake_run_action)
    monkeypatch.setattr(cohort_archive, "validate_account", _accept_account)

    result = await cohort_archive.run_cohort_archive(
        "alpaca", ACCT, _request(*_STOPPED_SIDS), operator_identity="op"
    )

    assert seen == [(sid, "archive", f"sweep-1:{sid}") for sid in _STOPPED_SIDS]
    assert [leg.outcome for leg in result.legs] == ["applied", "applied"]
    assert result.applied_count == 2
    assert result.receipt_id == "sweep-1"


# ── router-level integration against the real harness ────────────────────────


class _MixedLivenessRegistry(_FakeRegistry):
    """Two members are finished; one is still running."""

    def status(self, broker: str, sid: str) -> BotStatusView:
        view = super().status(broker, sid)
        if sid in _STOPPED_SIDS:
            return view.model_copy(
                update={
                    "running": False,
                    "phase": "OFF_DUTY",
                    "desired_state": "STOPPED",
                    "active_run_id": None,
                }
            )
        return view


@pytest.fixture()
async def archive_api(tmp_path: Path) -> AsyncIterator[FastAPI]:
    reset_broker_registry_for_testing()
    reset_idempotency_store_for_testing()
    set_active_clerk_runtime(None)
    sids = (*_STOPPED_SIDS, _RUNNING_SID)
    set_bot_task_registry(_MixedLivenessRegistry(tmp_path, sids=sids))  # type: ignore[arg-type]
    port = _FakeBrokerPort()
    get_broker_registry().register(port)  # type: ignore[arg-type]
    repo = ClerkSqliteRepository.initialize(account_id=ACCT, artifacts_root=tmp_path)
    for sid in sids:
        repo.register_strategy_instance(
            strategy_instance_id=sid,
            symbol="SPY",
            config_hash="config-1",
            strategy_key="deployment_validation",
            display_name="Deployment Validation",
            config_json=json.dumps(
                {"mode": "trade", "quantity": 1, "carryover_policy": "FORBID"}
            ),
        )
        submit_start_run(
            repo,
            account_id=ACCT,
            strategy_instance_id=sid,
            lifecycle_run_id=f"run-{sid}",
        )
    for sid in _STOPPED_SIDS:
        submit_stop_run(
            repo,
            account_id=ACCT,
            strategy_instance_id=sid,
            lifecycle_run_id=f"run-{sid}",
            operator_reason="finished",
        )
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=_FakeReadPort(positions=[]),  # type: ignore[arg-type]
        trade=_FakeTradePort(),  # type: ignore[arg-type]
    )
    await facade.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=facade))
    app = FastAPI()
    app.include_router(router)
    try:
        yield app
    finally:
        set_active_clerk_runtime(None)
        set_bot_task_registry(None)
        repo.close()
        reset_broker_registry_for_testing()
        reset_idempotency_store_for_testing()


async def test_the_view_presents_only_archive_candidates_with_real_leg_facts(
    archive_api: FastAPI,
) -> None:
    """A running bot is never a leg; a stopped flat one is armed.

    The prefilter is what keeps this read proportional to the candidates
    rather than to the roster — and the armed legs carry the per-bot panel's
    own token and revision, so the later POST executes what was presented.
    """
    async with httpx.AsyncClient(
        transport=ASGITransport(app=archive_api), base_url="http://t"
    ) as client:
        response = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/cohort-archive"
        )

    assert response.status_code == 200
    view = response.json()
    legs = [leg for cohort in view["cohorts"] for leg in cohort["legs"]]
    presented = {leg["strategy_instance_id"] for leg in legs}

    assert presented == set(_STOPPED_SIDS)
    assert _RUNNING_SID not in presented
    for leg in legs:
        assert leg["enabled"] is True
        assert leg["concurrency_token"]
        assert leg["revision"] is not None


async def test_a_single_member_group_is_still_presented(archive_api: FastAPI) -> None:
    """Unlike flatten, a lone finished bot must not be hidden.

    A flatten cohort of one is just the per-bot action, so ADR 0051 hides it.
    Here the surface's job is to clear the roster: hiding a lone finished bot
    would leave it unreachable from the only screen built to remove it.
    """
    async with httpx.AsyncClient(
        transport=ASGITransport(app=archive_api), base_url="http://t"
    ) as client:
        view = (
            await client.get(
                f"/api/brokers/alpaca/accounts/{ACCT}/bots/cohort-archive"
            )
        ).json()

    assert view["cohorts"], "an archivable roster must present at least one group"
    assert all(cohort["legs"] for cohort in view["cohorts"])
    assert sum(cohort["enabled_count"] for cohort in view["cohorts"]) == len(
        _STOPPED_SIDS
    )
