"""Backend-authored bot names and the Deploy submission ledger (#2551, PRD #2560)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport

from app.engine.live.identity import strategy_instance_artifact_dir
from app.engine.strategy.registry import (
    _STRATEGY_REGISTRY,
    StrategyCatalogError,
    validate_deploy_codes,
)
from app.schemas.deployment_budget import BudgetDeployCommandReceipt, DeployBudgetConsent
from app.schemas.exit_terms import ExitTermsInput
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_runner import AdmittedBotStart
from app.services.bot_runner_errors import UnknownBotError
from app.services.broker_v2_panel import budget_deploy
from app.services.broker_v2_panel.deploy_submissions import (
    BotNameUnavailable,
    DeploySubmissionConflict,
    DeploySubmissionLedger,
    bot_name,
)
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


def _claim(ledger: DeploySubmissionLedger, key: str, *, fingerprint: str = "settings-a", replaces: str | None = None):
    return ledger.claim(
        submission_key=key, symbol="SPY", strategy_key="ema_crossover_signal", request_fingerprint=fingerprint,
        replaces_strategy_instance_id=replaces, now_ms=_AT,
    )


def test_a_retry_with_the_same_key_returns_the_same_bot_and_claims_nothing_new(tmp_path: Path) -> None:
    ledger = DeploySubmissionLedger(tmp_path)
    first = _claim(ledger, "submission-a")

    again = _claim(ledger, "submission-a")

    assert again == first and first.strategy_instance_id == DEPLOYED_SID
    assert first.first_deployed_at_ms == _AT
    assert sorted(path.name for path in (tmp_path / "deploy_submissions" / "names").iterdir()) == [f"{first.strategy_instance_id}.json"]


def test_a_new_key_with_identical_settings_is_a_second_bot(tmp_path: Path) -> None:
    ledger = DeploySubmissionLedger(tmp_path)

    first, second = _claim(ledger, "submission-a"), _claim(ledger, "submission-b")

    assert (first.strategy_instance_id, second.strategy_instance_id) == (DEPLOYED_SID, f"{DEPLOYED_SID}-2")


def test_the_same_key_with_other_settings_is_refused(tmp_path: Path) -> None:
    ledger = DeploySubmissionLedger(tmp_path)
    _claim(ledger, "submission-a")

    with pytest.raises(DeploySubmissionConflict, match=DEPLOYED_SID):
        _claim(ledger, "submission-a", fingerprint="settings-b")


def test_two_concurrent_commits_in_one_minute_never_share_a_name(tmp_path: Path) -> None:
    # Two ledgers over one volume, as two request handlers each build their own.
    ledgers = [DeploySubmissionLedger(tmp_path), DeploySubmissionLedger(tmp_path)]
    start = Barrier(2)

    def claim(index: int) -> str:
        start.wait()
        return _claim(ledgers[index], f"concurrent-{index}").strategy_instance_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        names = sorted(pool.map(claim, range(2)))

    assert names == [DEPLOYED_SID, f"{DEPLOYED_SID}-2"]


def test_legacy_bots_are_never_renamed_and_their_ids_are_never_reused(tmp_path: Path) -> None:
    # A hand-named bot's durable directory, and one that happens to hold a generated-looking name.
    for sid in (*LEGACY_IDS, DEPLOYED_SID):
        strategy_instance_artifact_dir(tmp_path, "live_state", sid).mkdir(parents=True)
    ledger = DeploySubmissionLedger(tmp_path)

    claimed = _claim(ledger, "after-legacy")

    assert claimed.strategy_instance_id == f"{DEPLOYED_SID}-2"
    assert all(ledger.by_name(sid) is None for sid in LEGACY_IDS)
    assert all(strategy_instance_artifact_dir(tmp_path, "live_state", sid).is_dir() for sid in LEGACY_IDS)


# ── The receipt and the recovery read ───────────────────────────────────────


async def test_the_receipt_names_the_replaced_bot_and_the_first_deploy_instant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = DeploySubmissionLedger(tmp_path)
    claimed = _claim(ledger, "deploy-again", replaces=LEGACY_IDS[0])
    command = SimpleNamespace(command_id="cmd-1", state="accepted", updated_at_ms=_AT + 5)
    row = {"command_id": "cmd-1", "run_id": f"{claimed.strategy_instance_id}:run", "world": "real_live",
           "committed_cents": 80_000, "launched_at_ms": _AT + 5}
    repo = SimpleNamespace(write_fence=lambda: _NullFence(), deployment_budget=lambda sid: row if sid == claimed.strategy_instance_id else None,
                           get_command=lambda command_id: command)
    runtime = SimpleNamespace(sqlite_repository=repo)
    monkeypatch.setattr(budget_deploy, "get_bot_task_registry", lambda: SimpleNamespace(artifacts_root=tmp_path))

    receipt = budget_deploy._command_receipt(runtime, "LIVE-1", claimed.strategy_instance_id)

    assert receipt is not None and receipt.status == "deployed"
    assert receipt.strategy_instance_id == claimed.strategy_instance_id
    assert receipt.replaces_strategy_instance_id == LEGACY_IDS[0]
    assert receipt.first_deployed_at_ms == _AT
    assert receipt.message == f"{claimed.strategy_instance_id} is deployed"
    assert receipt.explanation == "$800.00 is set aside for it."

    async def recorded(account_id: str, sid: str) -> BudgetDeployCommandReceipt | None:
        return budget_deploy._command_receipt(runtime, account_id, sid)

    monkeypatch.setattr(budget_deploy, "command_receipt", recorded)
    assert await budget_deploy.submission_receipt("LIVE-1", "deploy-again") == receipt
    assert await budget_deploy.submission_receipt("LIVE-1", "never-submitted") is None


class _NullFence:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


# ── The route ───────────────────────────────────────────────────────────────


async def test_a_deploy_that_still_sends_a_name_is_refused_with_422(deploy_app) -> None:
    fast_app, registry = deploy_app

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots", json={**_BODY, "strategy_instance_id": "my-own-name"},
        )

    assert response.status_code == 422
    assert "assigned by the server" in response.text
    assert registry.deploy_calls == []


async def test_a_budgeted_deploy_resent_with_its_key_returns_the_recorded_bot_without_a_second_start(
    deploy_app, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A double-click, a reload or a lost response resends the key: one bot, one budget."""
    fast_app, registry = deploy_app
    monkeypatch.setattr("app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS", _ALLOW_BODY_STRATEGY)
    committed: dict[str, BudgetDeployCommandReceipt] = {}

    async def command_receipt(account_id: str, sid: str) -> BudgetDeployCommandReceipt | None:
        return committed.get(sid)

    def resolve_consent(account_id: str, request: object, *, resolved_parameters: dict) -> DeployBudgetConsent:
        return DeployBudgetConsent(committed_cents=100_000, risk_revision=1, actor="owner", request_fingerprint="f", world="real_paper")

    original_deploy = registry.deploy_with_admission

    async def deploy(**kwargs: object) -> AdmittedBotStart:
        started = await original_deploy(**kwargs)
        sid = str(kwargs["strategy_instance_id"])
        committed[sid] = BudgetDeployCommandReceipt(
            status="deployed", outcome="success", receipt_id="cmd", recorded_at_ms=_AT, command_id="cmd",
            strategy_instance_id=sid, run_id=f"{sid}:run", account_id=ACCT, world="real_paper", committed_usd="1000.00",
            message=f"{sid} is deployed", explanation="$1000.00 is set aside for it.", next_action="Open the bot's page.",
        )
        return started

    monkeypatch.setattr(budget_deploy, "command_receipt", command_receipt)
    monkeypatch.setattr(budget_deploy, "resolve_consent", resolve_consent)
    monkeypatch.setattr(registry, "deploy_with_admission", deploy)
    body = {**_BODY, "budget": {"amount_usd": "1000.00", "risk_revision": 1, "review_token": "reviewed"}}

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        first = await client.post(f"/api/brokers/alpaca/accounts/{ACCT}/bots", json=body)
        retry = await client.post(f"/api/brokers/alpaca/accounts/{ACCT}/bots", json=body)
        second_bot = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots", json={**body, "submission_key": "submission-0002"},
        )
        recovered = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/deploy-submissions/{_BODY['submission_key']}")
        unknown = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/deploy-submissions/never-submitted")

    assert first.status_code == retry.status_code == second_bot.status_code == 201
    assert first.json() == retry.json() and first.json()["strategy_instance_id"] == DEPLOYED_SID
    assert second_bot.json()["strategy_instance_id"] == f"{DEPLOYED_SID}-2"
    assert [call["strategy_instance_id"] for call in registry.deploy_calls] == [DEPLOYED_SID, f"{DEPLOYED_SID}-2"]
    assert recovered.status_code == 200 and recovered.json()["strategy_instance_id"] == DEPLOYED_SID
    assert unknown.status_code == 404 and "Nothing was set aside" in unknown.text


async def test_deploy_again_prefill_returns_the_sealed_settings_and_never_money(
    deploy_app, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fast_app, registry = deploy_app
    source = BrokerBotBinding(
        strategy_instance_id=LEGACY_IDS[1], strategy_key="ema_crossover_signal", broker="alpaca", symbol="SPY",
        mode="trade", quantity=3, action_plan=alpaca_v1_action_plan("SPY"),
        strategy_params={"symbol": "SPY", "gap": 0.2, "rsi_min": 50.0}, run_id="run-1", created_at_ms=0,
    )

    def binding_for_control(broker: str, sid: str) -> BrokerBotBinding:
        if sid != source.strategy_instance_id:
            raise UnknownBotError(f"No bot '{sid}' is bound to broker '{broker}'.", detail="Deploy the bot first.")
        return source

    async def sealed_exit_terms(account_id: str, sid: str):
        return ExitTermsInput(exit_allowance_bps=25, band_multiple=2, spread_cap_bps=50).seal()

    registry.binding_for_control = binding_for_control
    monkeypatch.setattr(budget_deploy, "sealed_exit_terms", sealed_exit_terms)

    async with httpx.AsyncClient(transport=ASGITransport(app=fast_app), base_url="http://test") as client:
        found = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{source.strategy_instance_id}/deploy-prefill")
        missing = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/nobody/deploy-prefill")

    assert found.status_code == 200
    assert found.json() == {
        "source_strategy_instance_id": LEGACY_IDS[1],
        "strategy_key": "ema_crossover_signal",
        "symbol": "SPY",
        "sizing": {"preset": "custom", "quantity": 3},
        "parameters": {"gap": 0.2, "rsi_min": 50.0},
        "exit_terms": {"band_multiple": 2.0, "spread_cap_bps": 50.0, "exit_allowance_bps": 25.0},
    }
    assert missing.status_code == 404
