"""Three-tier deploy-time parameter resolution (PRD Sec 10.3, #1728 Task 2).

Registered defaults -> symbol profile -> explicit operator override, resolved
once and sealed. ``resolve_deploy_strategy_params`` owns the merge;
``build_start_program_seal`` owns turning the resolved set into a seal whose
``parameters_match_validated_settings`` flag reflects the resolved *values*
regardless of which tier produced them.
"""

from __future__ import annotations

from app.schemas.run_admission import StrategyValidationAdmissionFact
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.broker_v2_panel.paper_deploy_service import resolve_deploy_strategy_params
from app.services.signal_program_admission import build_start_program_seal

_STRATEGY_KEY = "ema_crossover_signal"
_SYMBOL = "SPY"
_NOW = 1_787_400_000_000


def _validation() -> StrategyValidationAdmissionFact:
    return StrategyValidationAdmissionFact(
        state="VERIFIED",
        strategy_key=_STRATEGY_KEY,
        evidence_status="accepted",
        event_id="validation-profile-1",
        evidence_snapshot_sha256="b" * 64,
        verified_at_ms=_NOW,
        explanation="The exact validation snapshot was re-hashed.",
    )


def _binding(*, strategy_params: dict[str, object], origins: dict[str, str]) -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id="profile-tier-1",
        strategy_key=_STRATEGY_KEY,
        broker="alpaca",
        symbol=_SYMBOL,
        use_rth=True,
        mode="dry_run",
        quantity=1,
        carryover_policy="FORBID",
        action_plan=alpaca_v1_action_plan(_SYMBOL),
        strategy_params=strategy_params,
        strategy_param_origins=origins,
        sealed_account_id="sim:profile-tier-1",
        run_id="run-1",
        created_at_ms=_NOW,
    )


def test_resolve_deploy_strategy_params_is_a_clean_no_op_without_a_profile() -> None:
    resolved = resolve_deploy_strategy_params(_STRATEGY_KEY, _SYMBOL, {})

    assert resolved.effective["gap"] == 0.20
    assert resolved.origins["gap"] == "registered_default"
    assert resolved.diverges_from_defaults == ()


def test_resolve_deploy_strategy_params_symbol_profile_origin() -> None:
    resolved = resolve_deploy_strategy_params(
        _STRATEGY_KEY,
        _SYMBOL,
        {},
        symbol_profile={"gap": 0.35},
    )

    assert resolved.effective["gap"] == 0.35
    assert resolved.origins["gap"] == "deployment_symbol"
    # An untouched parameter still resolves to the registered default.
    assert resolved.origins["rsi_min"] == "registered_default"
    assert resolved.diverges_from_defaults == ("gap",)


def test_resolve_deploy_strategy_params_operator_override_beats_symbol_profile() -> None:
    resolved = resolve_deploy_strategy_params(
        _STRATEGY_KEY,
        _SYMBOL,
        {"gap": 0.5},
        symbol_profile={"gap": 0.35},
    )

    assert resolved.effective["gap"] == 0.5
    assert resolved.origins["gap"] == "deploy_override"


def test_seal_parameters_match_validated_settings_true_under_symbol_profile_tier() -> None:
    resolved = resolve_deploy_strategy_params(
        _STRATEGY_KEY,
        _SYMBOL,
        {},
        # The registered golden settings, but sourced from a symbol profile
        # rather than falling out of the schema's own defaults.
        symbol_profile={"gap": 0.20, "rsi_min": 50.0, "rsi_max": 70.0},
    )
    binding = _binding(strategy_params=resolved.effective, origins=resolved.origins)

    seal = build_start_program_seal(binding, _validation(), parameter_origins=resolved.origins)

    assert seal is not None
    assert seal.configured_signal.parameters["gap"].origin == "deployment_symbol"
    assert seal.configured_signal.parameters_match_validated_settings is True


def test_seal_parameters_match_validated_settings_false_under_symbol_profile_tier() -> None:
    resolved = resolve_deploy_strategy_params(
        _STRATEGY_KEY,
        _SYMBOL,
        {},
        symbol_profile={"gap": 0.35, "rsi_min": 50.0, "rsi_max": 70.0},
    )
    binding = _binding(strategy_params=resolved.effective, origins=resolved.origins)

    seal = build_start_program_seal(binding, _validation(), parameter_origins=resolved.origins)

    assert seal is not None
    assert seal.configured_signal.parameters["gap"].origin == "deployment_symbol"
    assert seal.configured_signal.parameters_match_validated_settings is False
