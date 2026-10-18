"""Exact setup re-proof and session scheduling compose with existing account gates."""

from types import SimpleNamespace

import pytest

from app.options_bots.golden import GoldenSetupError
from app.schemas.market_liveness import (
    MarketClockLivenessEvidence,
    SymbolMarketDataEvidence,
    SymbolTradingStatusEvidence,
)
from app.schemas.run_admission import StartWindowFact
from app.services.broker_v2_panel.panel_deploy import _option_choice_of
from app.services.broker_v2_panel.panel_errors import PanelRunnerError
from app.services.market_liveness import compose_market_liveness, liveness_blocks_entry
from app.services.options_bot_admission import options_start_facts
from app.services.run_admission import evaluate_run_admission
from tests.broker.alpaca.clerk.test_options_leg_custody import _session_binding, _setup
from tests.services.test_run_admission import _NOW, _bot, _clerk


async def test_exact_setup_can_use_a_later_explicit_reproof_without_rewriting_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.options_bots.golden as golden_module

    binding = _session_binding(_setup())

    async def ready(_identity: str):
        return SimpleNamespace(
            id=binding.options_bot.golden_setup_id, setup=binding.options_bot.setup,
            setup_sha256=binding.options_bot.setup_sha256, rules_digest="b" * 64,
            proof={"result_sha256": "c" * 64}, proof_sha256="d" * 64,
        )

    monkeypatch.setattr(golden_module, "get_ready_setup", ready)
    proof, validation = await options_start_facts(binding, verified_at_ms=_NOW)
    assert proof.state == "PROVEN" and validation.state == "VERIFIED"
    assert proof.running_artifact_digest == "b" * 64
    assert binding.options_bot.rules_digest == "a" * 64
    assert proof.golden_trace_root == "c" * 64
    facts = _bot().model_copy(update={
        "schedules_options_sessions": True, "program_build": proof, "validation": validation,
    })
    assert evaluate_run_admission(facts, _clerk(), evaluated_at_ms=_NOW).allowed


@pytest.mark.parametrize("mode", ["trade", "dry_run"])
async def test_revoked_setup_cannot_start_in_any_execution_world(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    import app.options_bots.golden as golden_module

    async def revoked(_identity: str):
        raise GoldenSetupError("golden_setup_revoked", "The owner revoked this setup.")

    monkeypatch.setattr(golden_module, "get_ready_setup", revoked)
    proof, validation = await options_start_facts(_session_binding(_setup()), verified_at_ms=_NOW - 1000)
    facts = _bot(mode=mode).model_copy(update={
        "schedules_options_sessions": True, "program_build": proof, "validation": validation,
    })
    decision = evaluate_run_admission(facts, _clerk(), evaluated_at_ms=_NOW)
    assert not decision.allowed
    assert decision.reason_code == "PROGRAM_BUILD_UNPROVEN"


def test_scheduled_options_can_deploy_while_market_closed_but_signal_and_halts_remain_gated() -> None:
    closed = _bot(liveness_state="CLOSED").model_copy(update={"start_window": StartWindowFact(state="CLOSED")})
    signal = evaluate_run_admission(closed, _clerk(), evaluated_at_ms=_NOW)
    options = evaluate_run_admission(closed.model_copy(update={"schedules_options_sessions": True}), _clerk(), evaluated_at_ms=_NOW)
    assert not signal.allowed and signal.reason_code == "DEPLOY_WINDOW_CLOSED"
    assert options.allowed
    halted = _bot(liveness_state="HALTED").model_copy(update={"schedules_options_sessions": True})
    assert not evaluate_run_admission(halted, _clerk(), evaluated_at_ms=_NOW).allowed


@pytest.mark.parametrize(
    ("phase", "clock", "connected", "data_state", "halted", "clock_age", "allowed"),
    [
        ("CLOSED", "CLOSED", True, "RECOVERING", False, 1000, True),
        ("CLOSED", "CLOSED", True, "STARTING", False, 1000, True),
        ("RTH", "CLOSED", True, "RECOVERING", False, 1000, False),
        ("CLOSED", "OPEN", True, "RECOVERING", False, 1000, False),
        ("CLOSED", "CLOSED", False, "RECOVERING", False, 1000, False),
        ("CLOSED", "CLOSED", True, "UNAVAILABLE", False, 1000, False),
        ("CLOSED", "CLOSED", True, "RECOVERING", True, 1000, False),
        ("CLOSED", "CLOSED", True, "RECOVERING", False, 6000, False),
    ],
)
def test_options_can_wait_for_a_session_without_weekend_ticks_but_keep_live_safety_gates(
    phase: str, clock: str, connected: bool, data_state: str, halted: bool, clock_age: int, allowed: bool,
) -> None:
    data = SymbolMarketDataEvidence(
        symbol="SPY", generation="weekend", state=data_state, observed_at_ms=_NOW - 1000,
        reason_code=f"MARKET_DATA_{data_state}", reason="Waiting for a current quote.",
    )
    liveness = compose_market_liveness(
        "SPY", now_ms=_NOW, connected=connected, connection_changed_at_ms=_NOW - 2000,
        market_clock=MarketClockLivenessEvidence(state=clock, source="alpaca.clock", observed_at_ms=_NOW - clock_age),
        symbol_status=SymbolTradingStatusEvidence(
            symbol="SPY", state="HALTED" if halted else "NOT_REPORTED", source="ibkr.market_data.status", observed_at_ms=_NOW - 1000,
        ),
        market_data=data, require_market_data=True,
    )
    bot = _bot(scheduled_phase=phase).model_copy(update={
        "schedules_options_sessions": True, "market_liveness": liveness,
        "market_data": _bot(scheduled_phase=phase).market_data.model_copy(update={"connected": connected}),
        "start_window": StartWindowFact(state="CLOSED"),
    })
    assert evaluate_run_admission(bot, _clerk(), evaluated_at_ms=_NOW).allowed is allowed
    # The scheduler's permission never authorizes immediate entries.
    assert liveness_blocks_entry(liveness, use_rth=True, extended_phase_proven=False, extended_session_live=False)
    assert not evaluate_run_admission(bot.model_copy(update={"schedules_options_sessions": False}), _clerk(), evaluated_at_ms=_NOW).allowed


def test_signal_deploy_prefill_refuses_an_options_session_with_a_typed_handoff() -> None:
    with pytest.raises(PanelRunnerError) as refusal:
        _option_choice_of(_session_binding(_setup()))
    assert refusal.value.reason_code == "OPTIONS_SETUP_DEPLOY_REQUIRED"
    assert refusal.value.http_status == 409
