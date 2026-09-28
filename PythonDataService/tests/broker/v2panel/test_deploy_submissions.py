"""Backend-authored bot names and the Deploy submission ledger (#2551, PRD #2560)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport

from app.broker.alpaca.clerk.sqlite.budget_projection import BudgetUnavailable
from app.engine.live.identity import strategy_instance_artifact_dir
from app.engine.strategy.registry import (
    _STRATEGY_REGISTRY,
    StrategyCatalogError,
    validate_deploy_codes,
)
from app.schemas.deployment_budget import BudgetDeployCommandReceipt, DeployBudgetConsent
from app.schemas.exit_terms import ExitTermsInput
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_runner import AdmittedBotStart, set_bot_task_registry
from app.services.bot_runner_errors import UnknownBotError
from app.services.broker_v2_panel import budget_deploy, deploy_submissions
from app.services.broker_v2_panel.deploy_submissions import (
    BotNameUnavailable,
    DeploySubmission,
    DeploySubmissionConflict,
    DeploySubmissionLedger,
    bot_name,
)
from app.services.broker_v2_panel.paper_deploy_service import resolve_deploy_strategy_params
from app.utils import timestamps
from tests.broker.v2panel.conftest import _BODY, DEPLOYED_SID
from tests.broker.v2panel.fixtures import ACCT

_ALLOW_BODY_STRATEGY = frozenset({("ema_crossover_signal", ACCT)})
#: The names bots carried before #2551, typed by hand (from the live lanes).
LEGACY_IDS = ("pt-ema-spy-0916", "paper-ema-spy-0924", "pt-dv-spy-0916", "alpaca-spy-01", "live-dry-dv-spy-0928")


def _ms(*args: int) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp() * 1000)


# ── The name ────────────────────────────────────────────────────────────────


def test_bot_name_is_symbol_code_date_and_new_york_minute_lowercase() -> None:
    at = _ms(2026, 9, 29, 13, 31, 42)  # 09:31:42 EDT

    assert bot_name("SPY", "ema_crossover_signal", at_ms=at) == "spy-ema-20260929-0931"
    assert bot_name("BRK.B", "deployment_validation", at_ms=at) == "brk.b-dv-20260929-0931"
    assert bot_name("SPY", "ema_crossover_signal", at_ms=at, ordinal=3) == "spy-ema-20260929-0931-3"


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        # Spring forward (2026-03-08): 14:31 UTC is 09:31 EST before it, 10:31 EDT after.
        (_ms(2026, 3, 6, 14, 31), "spy-ema-20260306-0931"),
        (_ms(2026, 3, 9, 14, 31), "spy-ema-20260309-1031"),
        (_ms(2026, 3, 9, 13, 31), "spy-ema-20260309-0931"),
        # Fall back (2026-11-01): 13:31 UTC is 09:31 EDT before it, 08:31 EST after.
        (_ms(2026, 10, 30, 13, 31), "spy-ema-20261030-0931"),
        (_ms(2026, 11, 2, 13, 31), "spy-ema-20261102-0831"),
        (_ms(2026, 11, 2, 14, 31), "spy-ema-20261102-0931"),
        # The ET date, not the UTC one: 00:30 UTC is still the previous evening in New York.
        (_ms(2026, 9, 30, 0, 30), "spy-ema-20260929-2030"),
    ],
)
def test_bot_name_reads_the_minute_through_the_new_york_zone_on_both_sides_of_dst(instant: int, expected: str) -> None:
    assert bot_name("SPY", "ema_crossover_signal", at_ms=instant) == expected


def test_a_name_that_cannot_fit_the_order_reference_cap_is_refused_not_truncated() -> None:
    with pytest.raises(BotNameUnavailable, match="order-reference limit"):
        bot_name("ABCDEFGHIJKL", "ema_crossover_signal", at_ms=_ms(2026, 9, 29, 13, 31))


# ── The catalog's codes ─────────────────────────────────────────────────────


def test_every_catalog_strategy_has_its_own_code() -> None:
    codes = [registration.deploy_code for registration in _STRATEGY_REGISTRY.values() if registration.catalog_visible]
    assert len(codes) == len(set(codes)) and all(codes)


def test_catalog_load_refuses_a_missing_or_duplicate_code() -> None:
    ema = _STRATEGY_REGISTRY["ema_crossover_signal"]
    with pytest.raises(StrategyCatalogError, match="needs a bot-name code"):
        validate_deploy_codes({"ema_crossover_signal": replace(ema, deploy_code="")})
    with pytest.raises(StrategyCatalogError, match="share the bot-name code 'ema'"):
        validate_deploy_codes({"a": ema, "b": replace(ema, display_name="Copy")})
    # A registration hidden from the catalog is never deployed, so never named.
    validate_deploy_codes({"a": ema, "hidden": replace(ema, deploy_code="", catalog_visible=False)})


# ── The ledger ──────────────────────────────────────────────────────────────

_AT = _ms(2026, 9, 25, 13, 31)
_DAY_MS = 86_400_000


def _claim(
    ledger: DeploySubmissionLedger, key: str, *, fingerprint: str = "settings-a", replaces: str | None = None,
    now_ms: int = _AT, renew: DeploySubmission | None = None,
) -> DeploySubmission:
    return ledger.claim(
        submission_key=key, symbol="SPY", strategy_key="ema_crossover_signal", request_fingerprint=fingerprint,
        replaces_strategy_instance_id=replaces, now_ms=now_ms, renew=renew,
    )


def _names(root: Path) -> list[str]:
    return sorted(path.stem for path in (root / "deploy_submissions" / "names").iterdir())


def test_a_retry_with_the_same_key_returns_the_same_bot_and_claims_nothing_new(tmp_path: Path) -> None:
    ledger = DeploySubmissionLedger(tmp_path)
    first = _claim(ledger, "submission-a")

    again = _claim(ledger, "submission-a")

    assert again == first and first.strategy_instance_id == DEPLOYED_SID
    assert first.claimed_at_ms == _AT
    assert _names(tmp_path) == [first.strategy_instance_id]


def test_a_new_key_with_identical_settings_is_a_second_bot(tmp_path: Path) -> None:
    ledger = DeploySubmissionLedger(tmp_path)

    first, second = _claim(ledger, "submission-a"), _claim(ledger, "submission-b")

    assert (first.strategy_instance_id, second.strategy_instance_id) == (DEPLOYED_SID, f"{DEPLOYED_SID}-2")


def test_the_same_key_with_other_settings_is_refused(tmp_path: Path) -> None:
    ledger = DeploySubmissionLedger(tmp_path)
    _claim(ledger, "submission-a")

    with pytest.raises(DeploySubmissionConflict, match=DEPLOYED_SID):
        _claim(ledger, "submission-a", fingerprint="settings-b")
    with pytest.raises(DeploySubmissionConflict, match=DEPLOYED_SID):
        ledger.recorded("submission-a", request_fingerprint="settings-b")


def test_a_claim_that_never_committed_is_renewed_from_the_retrys_minute_and_its_name_stays_burned(tmp_path: Path) -> None:
    ledger = DeploySubmissionLedger(tmp_path)
    refused = _claim(ledger, "submission-a", replaces="pt-ema-spy-0916")

    retried = _claim(ledger, "submission-a", replaces="pt-ema-spy-0916", now_ms=_AT + _DAY_MS, renew=refused)

    assert retried.strategy_instance_id == "spy-ema-20260926-0931"
    assert retried.claimed_at_ms == _AT + _DAY_MS and retried.replaces_strategy_instance_id == "pt-ema-spy-0916"
    assert ledger.by_key("submission-a") == retried
    # The abandoned name is never handed out again, even in its own minute.
    assert _claim(ledger, "submission-b").strategy_instance_id == f"{DEPLOYED_SID}-2"
    assert _names(tmp_path) == [DEPLOYED_SID, f"{DEPLOYED_SID}-2", "spy-ema-20260926-0931"]


def test_a_renewal_that_lost_to_another_renewal_takes_the_winners_name(tmp_path: Path) -> None:
    ledger = DeploySubmissionLedger(tmp_path)
    refused = _claim(ledger, "submission-a")
    winner = _claim(ledger, "submission-a", now_ms=_AT + 60_000, renew=refused)

    late = _claim(ledger, "submission-a", now_ms=_AT + 120_000, renew=refused)

    assert late == winner == ledger.by_key("submission-a")


# ── Races: the create-once files are the fence, not only the process lock ──


def _race(ledgers: list[DeploySubmissionLedger], keys: list[str]) -> list[DeploySubmission]:
    start = Barrier(len(keys))

    def claim(index: int) -> DeploySubmission:
        start.wait()
        return _claim(ledgers[index], keys[index])

    with ThreadPoolExecutor(max_workers=len(keys)) as pool:
        return list(pool.map(claim, range(len(keys))))


@pytest.mark.parametrize("process_lock", ["held", "absent"])
def test_concurrent_claims_in_one_minute_never_share_a_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, process_lock: str,
) -> None:
    # Separate ledgers over one volume, as two request handlers -- or two
    # processes, which share no lock -- each build their own.
    if process_lock == "absent":
        monkeypatch.setattr(deploy_submissions, "_CLAIM_LOCK", nullcontext())
    ledgers = [DeploySubmissionLedger(tmp_path) for _ in range(8)]

    claims = _race(ledgers, [f"concurrent-{index}" for index in range(8)])

    names = sorted(claim.strategy_instance_id for claim in claims)
    assert len(set(names)) == 8
    assert all(ledgers[0].by_key(f"concurrent-{index}") == claims[index] for index in range(8))


def test_concurrent_sends_of_one_key_hold_one_bot_without_the_process_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deploy_submissions, "_CLAIM_LOCK", nullcontext())
    ledgers = [DeploySubmissionLedger(tmp_path) for _ in range(8)]

    claims = _race(ledgers, ["one-submission"] * 8)

    assert len(set(claims)) == 1
    assert ledgers[0].by_key("one-submission") == claims[0]


def _another_process_publishes(ledger_root: Path, path: Callable[[DeploySubmissionLedger], Path], record: DeploySubmission):
    """``name_in_use`` that lets another process publish ``path`` between the free check and the create."""
    other = DeploySubmissionLedger(ledger_root)
    fired: list[bool] = []

    def name_in_use(_sid: str) -> bool:
        if not fired:
            fired.append(True)
            other._publish(path(other), record)
        return False

    return name_in_use


def test_a_name_another_process_takes_after_the_free_check_moves_this_claim_to_the_next_ordinal(tmp_path: Path) -> None:
    rival = DeploySubmission(submission_key="rival-submission", strategy_instance_id=DEPLOYED_SID, claimed_at_ms=_AT, request_fingerprint="other")
    ledger = DeploySubmissionLedger(tmp_path, name_in_use=_another_process_publishes(
        tmp_path, lambda other: other._name_path(DEPLOYED_SID), rival,
    ))

    claimed = _claim(ledger, "submission-a")

    assert claimed.strategy_instance_id == f"{DEPLOYED_SID}-2"


@pytest.mark.parametrize(("fingerprint", "outcome"), [("settings-a", "winner"), ("settings-b", "conflict")])
def test_a_key_another_process_claims_after_the_check_returns_its_bot_and_burns_this_name(
    tmp_path: Path, fingerprint: str, outcome: str,
) -> None:
    winner = DeploySubmission(submission_key="submission-a", strategy_instance_id="spy-ema-20260925-0930", claimed_at_ms=_AT, request_fingerprint="settings-a")
    ledger = DeploySubmissionLedger(tmp_path, name_in_use=_another_process_publishes(
        tmp_path, lambda other: other._attempt_path("submission-a", 1), winner,
    ))

    if outcome == "conflict":
        with pytest.raises(DeploySubmissionConflict, match="spy-ema-20260925-0930"):
            _claim(ledger, "submission-a", fingerprint=fingerprint)
    else:
        assert _claim(ledger, "submission-a", fingerprint=fingerprint) == winner
    # The name this claim reserved before losing the key is abandoned, never reused.
    assert _names(tmp_path) == [DEPLOYED_SID]
    assert _claim(ledger, "submission-b").strategy_instance_id == f"{DEPLOYED_SID}-2"


def test_legacy_bots_are_never_renamed_and_their_ids_are_never_reused(tmp_path: Path) -> None:
    # A hand-named bot's durable directory, and one that happens to hold a generated-looking name.
    for sid in (*LEGACY_IDS, DEPLOYED_SID):
        strategy_instance_artifact_dir(tmp_path, "live_state", sid).mkdir(parents=True)
    ledger = DeploySubmissionLedger(tmp_path)

    claimed = _claim(ledger, "after-legacy")

    assert claimed.strategy_instance_id == f"{DEPLOYED_SID}-2"
    assert _names(tmp_path) == [f"{DEPLOYED_SID}-2"]
    assert all(strategy_instance_artifact_dir(tmp_path, "live_state", sid).is_dir() for sid in LEGACY_IDS)


# ── The receipt ─────────────────────────────────────────────────────────────


def test_the_receipt_names_the_replaced_bot_and_dates_the_first_deploy_from_the_commit(tmp_path: Path) -> None:
    claimed = _claim(DeploySubmissionLedger(tmp_path), "deploy-again", replaces=LEGACY_IDS[0])
    committed_at = _AT + 1_234
    command = SimpleNamespace(command_id="cmd-1", state="accepted", updated_at_ms=_AT + 5)
    row = {"command_id": "cmd-1", "run_id": f"{claimed.strategy_instance_id}:run", "world": "real_live",
           "committed_cents": 80_000, "launched_at_ms": _AT + 5, "committed_at_ms": committed_at}
    repo = SimpleNamespace(write_fence=lambda: _NullFence(), deployment_budget=lambda sid: row if sid == claimed.strategy_instance_id else None,
                           get_command=lambda command_id: command)

    receipt = budget_deploy._command_receipt(SimpleNamespace(sqlite_repository=repo), "LIVE-1", claimed)

    assert receipt is not None and receipt.status == "deployed"
    assert receipt.strategy_instance_id == claimed.strategy_instance_id
    assert receipt.replaces_strategy_instance_id == LEGACY_IDS[0]
    # The custody commit's instant, not the instant the name was claimed.
    assert receipt.first_deployed_at_ms == committed_at != claimed.claimed_at_ms
    assert receipt.message == f"{claimed.strategy_instance_id} is deployed"
    assert receipt.explanation == "$800.00 is set aside for it."


class _NullFence:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


# ── The route ───────────────────────────────────────────────────────────────

_BUDGETED = {**_BODY, "budget": {"amount_usd": "1000.00", "risk_revision": 1, "review_token": "reviewed"}}
_BOTS = f"/api/brokers/alpaca/accounts/{ACCT}/bots"
_RECOVERY = f"/api/brokers/alpaca/accounts/{ACCT}/deploy-submissions"


def _receipt(sid: str, replaces: str | None = None) -> BudgetDeployCommandReceipt:
    return BudgetDeployCommandReceipt(
        status="deployed", outcome="success", receipt_id="cmd", recorded_at_ms=_AT, command_id="cmd",
        strategy_instance_id=sid, run_id=f"{sid}:run", account_id=ACCT, world="real_paper", committed_usd="1000.00",
        message=f"{sid} is deployed", explanation="$1000.00 is set aside for it.", next_action="Open the bot's page.",
        first_deployed_at_ms=_AT + 7, replaces_strategy_instance_id=replaces,
    )


@pytest.fixture
def budgeted(deploy_app, monkeypatch: pytest.MonkeyPatch):
    """The deploy harness with consent granted and custody commits recorded per name.

    ``refuse`` makes the next Deploy's commit refuse after the name is
    claimed; ``committed`` is custody's view, keyed by bot name.
    """
    fast_app, registry = deploy_app
    monkeypatch.setattr("app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS", _ALLOW_BODY_STRATEGY)
    committed: dict[str, BudgetDeployCommandReceipt] = {}
    refuse: list[BaseException] = []

    async def command_receipt(account_id: str, submission: DeploySubmission) -> BudgetDeployCommandReceipt | None:
        return committed.get(submission.strategy_instance_id)

    def resolve_consent(account_id: str, request: object, *, resolved_parameters: dict) -> DeployBudgetConsent:
        return DeployBudgetConsent(committed_cents=100_000, risk_revision=1, actor="owner", request_fingerprint="f", world="real_paper")

    original_deploy = registry.deploy_with_admission

    async def deploy(**kwargs: object) -> AdmittedBotStart:
        if refuse:
            registry.deploy_calls.append(kwargs)
            raise refuse.pop()
        started = await original_deploy(**kwargs)
        sid = str(kwargs["strategy_instance_id"])
        committed[sid] = _receipt(sid)
        return started

    monkeypatch.setattr(budget_deploy, "command_receipt", command_receipt)
    monkeypatch.setattr(budget_deploy, "resolve_consent", resolve_consent)
    monkeypatch.setattr(registry, "deploy_with_admission", deploy)
    return SimpleNamespace(app=fast_app, registry=registry, committed=committed, refuse=refuse)


def _client(fast_app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test")


async def test_a_deploy_that_still_sends_a_name_is_refused_with_422(deploy_app) -> None:
    fast_app, registry = deploy_app

    async with _client(fast_app) as client:
        response = await client.post(_BOTS, json={**_BODY, "strategy_instance_id": "my-own-name"})

    assert response.status_code == 422
    assert "assigned by the server" in response.text
    assert registry.deploy_calls == []


async def test_a_submission_key_outside_the_path_safe_shape_is_refused_by_the_schema(deploy_app) -> None:
    fast_app, registry = deploy_app

    async with _client(fast_app) as client:
        response = await client.post(_BOTS, json={**_BODY, "submission_key": "../../escape"})

    assert response.status_code == 422
    assert registry.deploy_calls == []


async def test_a_budgeted_deploy_resent_with_its_key_returns_the_recorded_bot_without_a_second_start(budgeted) -> None:
    """A double-click, a reload or a lost response resends the key: one bot, one budget."""
    async with _client(budgeted.app) as client:
        first = await client.post(_BOTS, json=_BUDGETED)
        retry = await client.post(_BOTS, json=_BUDGETED)
        second_bot = await client.post(_BOTS, json={**_BUDGETED, "submission_key": "submission-0002"})
        recovered = await client.get(f"{_RECOVERY}/{_BODY['submission_key']}")
        unknown = await client.get(f"{_RECOVERY}/never-submitted")

    assert first.status_code == retry.status_code == second_bot.status_code == 201
    assert first.json() == retry.json() and first.json()["strategy_instance_id"] == DEPLOYED_SID
    assert second_bot.json()["strategy_instance_id"] == f"{DEPLOYED_SID}-2"
    assert [call["strategy_instance_id"] for call in budgeted.registry.deploy_calls] == [DEPLOYED_SID, f"{DEPLOYED_SID}-2"]
    assert recovered.status_code == 200 and recovered.json() == first.json()
    assert recovered.json()["first_deployed_at_ms"] == _AT + 7
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "No Deploy was committed for this submission. Nothing was set aside and nothing started."


async def test_the_same_key_with_other_settings_is_refused_with_409_and_nothing_starts(budgeted) -> None:
    async with _client(budgeted.app) as client:
        first = await client.post(_BOTS, json=_BUDGETED)
        changed = await client.post(_BOTS, json={**_BUDGETED, "budget": {**_BUDGETED["budget"], "amount_usd": "800.00"}})

    assert first.status_code == 201
    assert changed.status_code == 409
    assert changed.json()["detail"]["message"] == "This Deploy was already sent with other settings."
    assert changed.json()["detail"]["why"] == (
        f"This Deploy was already sent with different settings as {DEPLOYED_SID}. Nothing new was set aside or started."
    )
    assert changed.json()["detail"]["next_action"] == "Start a new Deploy from the form."
    assert len(budgeted.registry.deploy_calls) == 1


async def test_a_deploy_refused_before_the_commit_checks_claims_no_name(budgeted, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse_consent(account_id: str, request: object, *, resolved_parameters: dict) -> DeployBudgetConsent:
        raise BudgetUnavailable("Type the displayed account and dollar confirmation before Live Deploy.")

    monkeypatch.setattr(budget_deploy, "resolve_consent", refuse_consent)

    async with _client(budgeted.app) as client:
        refused = await client.post(_BOTS, json=_BUDGETED)
        recovered = await client.get(f"{_RECOVERY}/{_BODY['submission_key']}")

    assert refused.status_code == 409
    assert refused.json()["detail"]["why"] == "Type the displayed account and dollar confirmation before Live Deploy."
    assert budgeted.registry.deploy_calls == []
    assert not (budgeted.registry.artifacts_root / "deploy_submissions").exists()
    assert recovered.status_code == 404


async def test_a_deploy_refused_at_commit_and_retried_later_with_its_key_is_named_from_the_retrys_minute(
    budgeted, monkeypatch: pytest.MonkeyPatch,
) -> None:
    budgeted.refuse.append(BudgetUnavailable("Wait for a fresh IBKR price, then review Deploy again."))

    async with _client(budgeted.app) as client:
        refused = await client.post(_BOTS, json=_BUDGETED)
        not_committed = await client.get(f"{_RECOVERY}/{_BODY['submission_key']}")
        # The owner fixes the cause and presses Deploy again on the next session day at 09:25 ET.
        next_day = _ms(2026, 9, 28, 13, 25)
        monkeypatch.setattr(timestamps, "time", SimpleNamespace(time=lambda: next_day / 1000))
        retried = await client.post(_BOTS, json=_BUDGETED)
        committed = await client.get(f"{_RECOVERY}/{_BODY['submission_key']}")

    assert refused.status_code == 409
    assert refused.json()["detail"]["why"] == "Wait for a fresh IBKR price, then review Deploy again."
    assert not_committed.status_code == 200 and not_committed.json() == {
        "status": "not_committed",
        "submission_key": _BODY["submission_key"],
        "strategy_instance_id": DEPLOYED_SID,
        "claimed_at_ms": not_committed.json()["claimed_at_ms"],
        "message": f"{DEPLOYED_SID} was not deployed",
        "explanation": "Its Deploy never committed, so nothing was set aside for it.",
        "next_action": "Deploy again when ready; the bot is named from the minute you do.",
    }
    assert retried.status_code == 201, retried.text
    assert retried.json()["strategy_instance_id"] == "spy-ema-20260928-0925"
    assert [call["strategy_instance_id"] for call in budgeted.registry.deploy_calls] == [DEPLOYED_SID, "spy-ema-20260928-0925"]
    assert committed.json() == retried.json()


async def test_the_recovery_read_says_a_deploy_being_sent_is_in_flight_and_a_resend_starts_nothing(budgeted) -> None:
    release = asyncio.Event()
    entered = asyncio.Event()
    deploy = budgeted.registry.deploy_with_admission

    async def slow_deploy(**kwargs: object) -> AdmittedBotStart:
        entered.set()
        await release.wait()
        return await deploy(**kwargs)

    budgeted.registry.deploy_with_admission = slow_deploy

    async with _client(budgeted.app) as client:
        sending = asyncio.create_task(client.post(_BOTS, json=_BUDGETED))
        await asyncio.wait_for(entered.wait(), timeout=5)
        in_flight = await client.get(f"{_RECOVERY}/{_BODY['submission_key']}")
        # Bounded: a resend that started a second Deploy would wait on the first forever.
        resent = await asyncio.wait_for(client.post(_BOTS, json=_BUDGETED), timeout=5)
        release.set()
        sent = await sending

    assert in_flight.status_code == 200
    assert in_flight.json()["status"] == "in_flight" and in_flight.json()["strategy_instance_id"] == DEPLOYED_SID
    assert in_flight.json()["message"] == f"{DEPLOYED_SID} is being deployed now"
    assert resent.status_code == 409 and resent.json()["detail"]["message"] == "This Deploy is already being sent."
    assert sent.status_code == 201 and sent.json()["strategy_instance_id"] == DEPLOYED_SID
    assert [call["strategy_instance_id"] for call in budgeted.registry.deploy_calls] == [DEPLOYED_SID]


async def test_the_recovery_read_without_a_bot_runner_is_unavailable_not_a_denial(budgeted) -> None:
    async with _client(budgeted.app) as client:
        await client.post(_BOTS, json=_BUDGETED)
        set_bot_task_registry(None)
        response = await client.get(f"{_RECOVERY}/{_BODY['submission_key']}")

    assert response.status_code == 503
    assert response.json()["detail"]["message"] == "The bot runner is not available."
    assert "Nothing was set aside" not in response.text


# ── Deploy again ────────────────────────────────────────────────────────────


def _sealed_source(strategy_key: str, sid: str, overrides: dict[str, object], *, sealed: bool = True) -> BrokerBotBinding:
    """A bot deployed with ``overrides``, as the binding repository reloads it."""
    resolved = resolve_deploy_strategy_params(strategy_key, "SPY", overrides)
    return BrokerBotBinding(
        strategy_instance_id=sid, strategy_key=strategy_key, broker="alpaca", symbol="SPY",
        mode="trade", quantity=3, action_plan=alpaca_v1_action_plan("SPY"),
        strategy_params=resolved.effective, strategy_param_origins=resolved.origins if sealed else None,
        run_id="run-1", created_at_ms=0,
    )


@pytest.fixture
def prefill_app(deploy_app, monkeypatch: pytest.MonkeyPatch):
    fast_app, registry = deploy_app
    sources: dict[str, BrokerBotBinding] = {}

    def binding_for_control(broker: str, sid: str) -> BrokerBotBinding:
        if sid not in sources:
            raise UnknownBotError(f"No bot '{sid}' is bound to broker '{broker}'.", detail="Deploy the bot first.")
        return sources[sid]

    def status(broker: str, sid: str) -> object:
        return binding_for_control(broker, sid)

    async def sealed_exit_terms(account_id: str, sid: str):
        return ExitTermsInput(exit_allowance_bps=25, band_multiple=2, spread_cap_bps=50).seal()

    registry.binding_for_control = binding_for_control
    registry.status = status
    monkeypatch.setattr(budget_deploy, "sealed_exit_terms", sealed_exit_terms)
    return SimpleNamespace(app=fast_app, registry=registry, sources=sources)


async def test_deploy_again_prefill_returns_the_sealed_settings_and_never_money(prefill_app) -> None:
    source = _sealed_source("ema_crossover_signal", LEGACY_IDS[1], {"gap": 0.3, "rsi_min": 55.0})
    prefill_app.sources[source.strategy_instance_id] = source

    async with _client(prefill_app.app) as client:
        found = await client.get(f"{_BOTS}/{source.strategy_instance_id}/deploy-prefill")
        missing = await client.get(f"{_BOTS}/nobody/deploy-prefill")
        elsewhere = await client.get(f"/api/brokers/alpaca/accounts/PA-OTHER/bots/{source.strategy_instance_id}/deploy-prefill")

    assert found.status_code == 200
    assert found.json() == {
        "source_strategy_instance_id": LEGACY_IDS[1],
        "strategy_key": "ema_crossover_signal",
        "symbol": "SPY",
        "sizing": {"preset": "custom", "quantity": 3},
        # Only what the owner set: the registered defaults are not re-sent as overrides.
        "parameters": {"gap": 0.3, "rsi_min": 55.0},
        "exit_terms": {"band_multiple": 2.0, "spread_cap_bps": 50.0, "exit_allowance_bps": 25.0},
    }
    assert missing.status_code == 404
    # Another account's path finds no such bot here, rather than reading custody for it.
    assert elsewhere.status_code == 404


@pytest.mark.parametrize(("strategy_key", "overrides", "sealed"), [
    ("ema_crossover_signal", {"gap": 0.3, "rsi_max": 65.0}, True),
    ("ema_crossover_signal", {}, True),
    ("deployment_validation", {}, True),
    ("deployment_validation", {}, False),
])
async def test_deploy_again_prefill_resolves_to_the_same_params_and_origins(
    prefill_app, strategy_key: str, overrides: dict[str, object], sealed: bool,
) -> None:
    """The prefill is a valid Deploy request: never a hidden parameter, never a default posing as an override."""
    source = _sealed_source(strategy_key, "pt-src-0916", overrides, sealed=sealed)
    prefill_app.sources[source.strategy_instance_id] = source

    async with _client(prefill_app.app) as client:
        prefill = (await client.get(f"{_BOTS}/{source.strategy_instance_id}/deploy-prefill")).json()

    again = resolve_deploy_strategy_params(strategy_key, prefill["symbol"], prefill["parameters"])
    assert again.effective == source.strategy_params
    if sealed:
        assert again.origins == source.strategy_param_origins


async def test_deploy_again_prefill_is_accepted_by_preview_and_deploy_with_the_sources_resolution(
    prefill_app, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS", _ALLOW_BODY_STRATEGY)
    source = _sealed_source("ema_crossover_signal", LEGACY_IDS[1], {"gap": 0.3})
    prefill_app.sources[source.strategy_instance_id] = source
    previews: list[dict] = []
    preview = prefill_app.registry.preview_start_admission

    async def recording_preview(**kwargs: object):
        previews.append(kwargs)
        return await preview(**kwargs)

    prefill_app.registry.preview_start_admission = recording_preview

    async with _client(prefill_app.app) as client:
        prefill = (await client.get(f"{_BOTS}/{source.strategy_instance_id}/deploy-prefill")).json()
        settings = {
            "strategy_key": prefill["strategy_key"], "symbol": prefill["symbol"], "sizing": prefill["sizing"],
            "parameters": prefill["parameters"], "exit_terms": prefill["exit_terms"],
        }
        previewed = await client.post(f"{_BOTS}/admission", json=settings)
        deployed = await client.post(_BOTS, json={
            **settings, "submission_key": "deploy-again-0001", "replaces_strategy_instance_id": source.strategy_instance_id,
        })

    assert previewed.status_code == 200 and deployed.status_code == 201
    for call in (previews[-1], prefill_app.registry.deploy_calls[-1]):
        assert call["strategy_params"] == source.strategy_params
        assert call["strategy_param_origins"] == source.strategy_param_origins
