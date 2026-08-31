"""Cohort-scoped flatten (ADR 0051, #1802): presentation + batch execution.

Two layers on purpose. The service-level tests pin the orchestration
contract (derived per-leg idempotency identity, typed per-leg outcomes,
continue-past-refusal, account-scoped early exit) against a monkeypatched
per-bot pipeline. The router-level tests run the real harness — real
``ClerkSqliteRepository``, real facade, real router — so the presented leg
facts are the per-bot panel's actual presented action facts, and a POST of a
blocked leg comes back as a typed refusal without aborting its siblings.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

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
from app.schemas.broker_v2_panel import (
    CohortFlattenLegRequest,
    CohortFlattenRequest,
    PanelActionResult,
)
from app.services.bot_runner import set_bot_task_registry
from app.services.broker_v2_panel import cohort_flatten, panel_data_source
from app.services.broker_v2_panel.action_execution_service import (
    ActionOutcomeUnknownError,
    ExecutionAuthorityLostError,
    StaleRevisionError,
    reset_idempotency_store_for_testing,
)
from tests.broker.alpaca.clerk.sqlite.conftest import (
    _broker_position_fixture,
    _FakeReadPort,
    _FakeTradePort,
    _make_held_position,
)
from tests.broker.v2panel.fixtures import ACCT
from tests.broker.v2panel.test_panel_router import _FakeBrokerPort, _FakeRegistry

_COHORT_SIDS = ("qq-bot-1", "qq-bot-2", "qq-bot-3")
_LONER_SID = "solo-bot-1"
# The first cohort member is stranded in T3's exact state: run stopped with
# reconciled attributed exposure, which arms the recovery ladder's
# execute_safe_flatten on its panel.
_STRANDED_SID = _COHORT_SIDS[0]


def _leg(sid: str, token: str = "token-1") -> CohortFlattenLegRequest:
    return CohortFlattenLegRequest(
        strategy_instance_id=sid,
        action_id="flatten_stop",
        revision=1,
        concurrency_token=token,
    )


def _request(*sids: str) -> CohortFlattenRequest:
    return CohortFlattenRequest(
        idempotency_key="wave-1",
        reason="cohort stop wave",
        legs=[_leg(sid) for sid in sids],
    )


def _applied(action_id: str = "flatten_stop", *, applied: bool = True) -> PanelActionResult:
    return PanelActionResult(
        action_id=action_id,  # type: ignore[arg-type]
        receipt_id="receipt-1",
        recorded_at_ms=1,
        applied=applied,
        revision=1,
        concurrency_token="token-1",
        message="done",
    )


# ── service-level orchestration contract ─────────────────────────────────────


async def test_batch_derives_per_leg_identity_and_maps_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str]] = []

    async def fake_run_action(broker, account_id, sid, request, *, operator_identity):
        seen.append((sid, request.idempotency_key))
        return _applied(applied=(sid != _COHORT_SIDS[1]))

    monkeypatch.setattr(panel_data_source, "run_action", fake_run_action)
    monkeypatch.setattr(
        cohort_flatten, "validate_account", _accept_account, raising=True
    )

    result = await cohort_flatten.run_cohort_flatten(
        "alpaca", ACCT, _request(*_COHORT_SIDS), operator_identity="op"
    )

    # Distinct, derived idempotency identity per leg (#1752 US11).
    assert seen == [(sid, f"wave-1:{sid}") for sid in _COHORT_SIDS]
    assert [leg.outcome for leg in result.legs] == ["applied", "replayed", "applied"]
    assert result.applied_count == 2
    assert result.replayed_count == 1
    assert result.receipt_id == "wave-1"


async def _accept_account(broker: str, account_id: str) -> str:
    return account_id


async def test_a_refused_leg_does_not_abort_its_siblings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_action(broker, account_id, sid, request, *, operator_identity):
        if sid == _COHORT_SIDS[1]:
            raise StaleRevisionError(
                "This action changed since it was presented.",
                detail="stale token",
            )
        return _applied()

    monkeypatch.setattr(panel_data_source, "run_action", fake_run_action)
    monkeypatch.setattr(cohort_flatten, "validate_account", _accept_account)

    result = await cohort_flatten.run_cohort_flatten(
        "alpaca", ACCT, _request(*_COHORT_SIDS), operator_identity="op"
    )

    outcomes = {leg.strategy_instance_id: leg.outcome for leg in result.legs}
    assert outcomes == {
        _COHORT_SIDS[0]: "applied",
        _COHORT_SIDS[1]: "refused",
        _COHORT_SIDS[2]: "applied",
    }
    refused = result.legs[1]
    assert refused.error is not None
    assert refused.error.outcome == "conflict"
    assert refused.error.why == "stale token"
    assert result.refused_count == 1


async def test_unknown_outcome_keeps_the_leg_receipt_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_action(broker, account_id, sid, request, *, operator_identity):
        raise ActionOutcomeUnknownError(
            "The command did not return a terminal receipt.",
            detail="inspect Clerk evidence",
        )

    monkeypatch.setattr(panel_data_source, "run_action", fake_run_action)
    monkeypatch.setattr(cohort_flatten, "validate_account", _accept_account)

    result = await cohort_flatten.run_cohort_flatten(
        "alpaca", ACCT, _request(_COHORT_SIDS[0]), operator_identity="op"
    )

    leg = result.legs[0]
    assert leg.outcome == "unknown"
    assert leg.error is not None
    assert leg.error.outcome == "unknown"
    # The receipt id names the derived per-leg key: the operator inspects
    # Clerk evidence for exactly this identity before minting a new one.
    assert leg.error.receipt_id == f"wave-1:{_COHORT_SIDS[0]}"
    assert result.failed_count == 1


async def test_account_scoped_authority_loss_ends_the_batch_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted: list[str] = []

    async def fake_run_action(broker, account_id, sid, request, *, operator_identity):
        attempted.append(sid)
        if sid == _COHORT_SIDS[1]:
            raise ExecutionAuthorityLostError()
        return _applied()

    monkeypatch.setattr(panel_data_source, "run_action", fake_run_action)
    monkeypatch.setattr(cohort_flatten, "validate_account", _accept_account)

    result = await cohort_flatten.run_cohort_flatten(
        "alpaca", ACCT, _request(*_COHORT_SIDS), operator_identity="op"
    )

    # The third leg was never attempted: an account-scoped fact, not a
    # per-leg refusal, ends the batch (ADR 0051).
    assert attempted == list(_COHORT_SIDS[:2])
    assert [leg.strategy_instance_id for leg in result.legs] == list(_COHORT_SIDS[:2])
    assert result.legs[1].outcome == "failed"
    assert result.legs[1].error is not None
    assert result.legs[1].error.reason_code is not None


def test_request_rejects_duplicate_legs_and_oversized_identity() -> None:
    with pytest.raises(ValueError, match="distinct bots"):
        CohortFlattenRequest(
            idempotency_key="wave-1",
            legs=[_leg("bot-a"), _leg("bot-a")],
        )
    with pytest.raises(ValueError, match="identity budget"):
        CohortFlattenRequest(
            idempotency_key="k" * 64,
            legs=[_leg("s" * 96)],
        )


# ── router-level integration against the real harness ────────────────────────


class _MixedLivenessRegistry(_FakeRegistry):
    """The stranded member is genuinely stopped; its siblings run."""

    def status(self, broker, sid):
        view = super().status(broker, sid)
        if sid == _STRANDED_SID:
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
async def cohort_api(tmp_path):
    reset_broker_registry_for_testing()
    reset_idempotency_store_for_testing()
    set_active_clerk_runtime(None)
    sids = (*_COHORT_SIDS, _LONER_SID)
    set_bot_task_registry(_MixedLivenessRegistry(tmp_path, sids=sids))  # type: ignore[arg-type]
    port = _FakeBrokerPort()
    get_broker_registry().register(port)  # type: ignore[arg-type]
    repo = ClerkSqliteRepository.initialize(account_id=ACCT, artifacts_root=tmp_path)
    for sid in sids:
        repo.register_strategy_instance(
            strategy_instance_id=sid,
            # _make_held_position enters SPY, so the cohort trades SPY.
            symbol="SPY" if sid in _COHORT_SIDS else "TSLA",
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
    # T3's stranded state for the first member: a filled, attributed entry,
    # then a stop — leaving exposure the recovery ladder must flatten.
    await _make_held_position(
        repo, account_id=ACCT, strategy_instance_id=_STRANDED_SID, run_id=f"run-{_STRANDED_SID}"
    )
    submit_stop_run(
        repo,
        account_id=ACCT,
        strategy_instance_id=_STRANDED_SID,
        lifecycle_run_id=f"run-{_STRANDED_SID}",
        operator_reason="cohort-stop-wave",
    )
    # The facade's broker ports see exactly the attributed position, so the
    # reconciliation below lands clean and the ladder's flatten arms.
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=_FakeReadPort(positions=[_broker_position_fixture("SPY", quantity=10.0)]),  # type: ignore[arg-type]
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


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_presentation_groups_multi_member_cohorts_with_real_leg_facts(
    cohort_api: FastAPI,
) -> None:
    async with _client(cohort_api) as client:
        response = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/cohort-flatten"
        )
        assert response.status_code == 200, response.text
        view = response.json()

        # Only the three-member SPY cohort qualifies; the TSLA loner does not.
        assert [c["symbol"] for c in view["cohorts"]] == ["SPY"]
        cohort = view["cohorts"][0]
        assert cohort["strategy_key"] == "deployment_validation"
        assert [leg["strategy_instance_id"] for leg in cohort["legs"]] == list(
            _COHORT_SIDS
        )

        # T3's stranded member — stopped with reconciled attributed exposure —
        # presents an ARMED recovery-ladder flatten with its blast radius from
        # the same panel cut as the token.
        by_sid = {leg["strategy_instance_id"]: leg for leg in cohort["legs"]}
        stranded = by_sid[_STRANDED_SID]
        assert stranded["action_id"] == "execute_safe_flatten"
        assert stranded["enabled"] is True
        assert stranded["concurrency_token"]
        assert stranded["exposure"] == {"SPY": 10.0}
        assert cohort["enabled_count"] == 1
        # Running members present disabled — the ladder is stop first, then
        # flatten; an armed flatten on a running member would be a lie.
        for sid in _COHORT_SIDS[1:]:
            assert by_sid[sid]["enabled"] is False

        # Every presented leg fact is the per-bot panel's own presented
        # flatten action — token, revision, enabled — never synthesized.
        for leg in cohort["legs"]:
            panel = await client.get(
                f"/api/brokers/alpaca/accounts/{ACCT}/bots/{leg['strategy_instance_id']}/panel"
            )
            assert panel.status_code == 200
            actions = {a["action_id"]: a for a in panel.json()["actions"]}
            if leg["action_id"] is None:
                assert not (
                    set(actions) & {"flatten_stop", "execute_safe_flatten"}
                ), "a presented panel flatten action was dropped from the leg"
                assert leg["enabled"] is False
                continue
            presented = actions[leg["action_id"]]
            assert leg["enabled"] == presented["enabled"]
            assert leg["concurrency_token"] == presented["concurrency_token"]
            assert leg["revision"] == presented["revision"]
            if not presented["enabled"]:
                assert leg["enabled"] is False


async def test_posting_legs_round_trips_typed_per_leg_outcomes(
    cohort_api: FastAPI,
) -> None:
    """POST every presented leg end-to-end through the real pipeline.

    The stranded member's armed recovery-ladder flatten genuinely APPLIES
    (real repo, real recovery dispatcher, fake broker ports); its running
    siblings' disabled legs come back as typed refusals — and crucially each
    leg answers individually instead of the first refusal aborting the batch
    (#1802's constraint, proven against the real router)."""
    async with _client(cohort_api) as client:
        view = (
            await client.get(
                f"/api/brokers/alpaca/accounts/{ACCT}/bots/cohort-flatten"
            )
        ).json()
        presented = [
            leg for leg in view["cohorts"][0]["legs"] if leg["action_id"] is not None
        ]
        assert presented, "the harness must present at least the stranded leg"
        legs = [
            {
                "strategy_instance_id": leg["strategy_instance_id"],
                "action_id": leg["action_id"],
                "revision": leg["revision"],
                "concurrency_token": leg["concurrency_token"],
            }
            for leg in presented
        ]

        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/cohort-flatten",
            json={
                "idempotency_key": "wave-e2e",
                "reason": "cohort validation",
                "legs": legs,
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()

        # Every leg answered, in request order, each with a typed outcome.
        assert [leg["strategy_instance_id"] for leg in result["legs"]] == [
            leg["strategy_instance_id"] for leg in legs
        ]
        outcomes = {
            leg["strategy_instance_id"]: leg["outcome"] for leg in result["legs"]
        }
        # The armed stranded leg executed for real through the recovery
        # dispatcher, with its own receipt...
        assert outcomes[_STRANDED_SID] == "applied"
        applied = next(
            leg for leg in result["legs"]
            if leg["strategy_instance_id"] == _STRANDED_SID
        )
        assert applied["result"] is not None
        assert applied["result"]["receipt_id"]
        # ...and its disabled siblings refused with typed errors, without
        # aborting the batch.
        for leg in result["legs"]:
            if leg["strategy_instance_id"] == _STRANDED_SID:
                continue
            assert leg["outcome"] in {"refused", "failed", "unknown"}
            assert leg["error"] is not None
            assert leg["error"]["message"]
        assert result["applied_count"] == 1
        assert result["receipt_id"] == "wave-e2e"
