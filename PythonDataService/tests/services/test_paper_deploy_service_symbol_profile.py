"""Three-tier deploy-time parameter resolution (#1728 Task 2).

Registered defaults -> symbol profile -> explicit operator override, resolved
once and sealed. ``resolve_deploy_strategy_params`` owns the merge;
``build_start_program_seal`` owns turning the resolved set into a seal whose
``parameters_match_validated_settings`` flag reflects the resolved *values*
regardless of which tier produced them.
"""

from __future__ import annotations

import pytest

from app.engine.indicators.macd import MovingAverageConvergenceDivergence
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.marketdata.feed import MAX_WARMUP_LOOKBACK_DAYS
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
        symbol_profile={"gap": 0.2, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0},
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
        symbol_profile={"gap": 0.0, "gap_bps": 0.0, "rsi_min": 30.0, "rsi_max": 70.0},
    )
    binding = _binding(strategy_params=resolved.effective, origins=resolved.origins)

    seal = build_start_program_seal(binding, _validation(), parameter_origins=resolved.origins)

    assert seal is not None
    assert seal.configured_signal.parameters["gap"].origin == "deployment_symbol"
    assert seal.configured_signal.parameters_match_validated_settings is False


def test_qualified_presets_resolve_to_the_exact_registry_corpus_without_changing_it() -> None:
    from app.engine.strategy.registry import _STRATEGY_REGISTRY
    from app.research.golden_search.qualifications import registry_point_matches
    from app.services.broker_v2_panel.paper_deploy_service import _qualified_configuration

    for key, registration in _STRATEGY_REGISTRY.items():
        contract = registration.signal_program_contract
        if contract is None:
            continue
        before = dict(contract.validated_settings)
        preset = _qualified_configuration(key, "NOT-COVERED")
        assert preset is not None
        assert registry_point_matches(contract, {**preset.parameters, "symbol": preset.symbol})
        assert contract.validated_settings == before
        assert "symbol" not in preset.parameters
        # The form submits the preset as it stands, so Deploy must take it (#2841).
        resolve_deploy_strategy_params(key, preset.symbol, dict(preset.parameters))
        for symbol in contract.validated_symbols:
            assert _qualified_configuration(key, symbol).symbol == symbol


@pytest.mark.parametrize("strategy_key", ["spy_strategy_a", "spy_strategy_b"])
@pytest.mark.parametrize(("fast", "slow", "builds"), [(25, 26, True), (26, 26, False), (30, 26, False)])
def test_deploy_refuses_exactly_the_macd_periods_the_indicator_cannot_build(
    strategy_key: str, fast: int, slow: int, builds: bool
) -> None:
    """#2841: each period passed its own bounds, so the deploy sealed and the bot then raised on start.

    The rule is the indicator's own, so each case first asks the indicator.
    """
    overrides = {"macd_fast": fast, "macd_slow": slow}
    if builds:
        MovingAverageConvergenceDivergence("probe", fast, slow, 9)
        assert resolve_deploy_strategy_params(strategy_key, _SYMBOL, overrides).effective["macd_fast"] == fast
        return
    with pytest.raises(ValueError, match="must be < slow_period"):
        MovingAverageConvergenceDivergence("probe", fast, slow, 9)
    with pytest.raises(ValueError, match=rf"macd_fast \({fast}\) must be less than macd_slow \({slow}\)"):
        resolve_deploy_strategy_params(strategy_key, _SYMBOL, overrides)


@pytest.mark.parametrize(
    ("long_window", "lookback_days", "loads"),
    [(200, 18, True), (258, 20, True), (259, 24, False), (500, 39, False)],
    ids=["sma_50_200", "at_the_limit", "first_past_the_limit", "long_past_the_limit"],
)
def test_deploy_refuses_exactly_the_periods_whose_warmup_history_a_bot_cannot_load(
    long_window: int, lookback_days: int, loads: bool
) -> None:
    """#2841: a lookback longer than any history request that returns was sealed, then refused at Start.

    SMA on its default 15-minute bars. Each case first asks the contract how
    many days these periods would seal.
    """
    registration = _STRATEGY_REGISTRY["sma_crossover"]
    contract = registration.signal_program_contract
    assert contract is not None
    overrides = {"short_window": 50, "long_window": long_window}
    assert contract.resolved_warmup_lookback_days(registration.param_schema(**overrides)) == lookback_days

    if loads:
        assert resolve_deploy_strategy_params("sma_crossover", _SYMBOL, overrides).effective["long_window"] == long_window
        return
    with pytest.raises(ValueError, match=rf"need {lookback_days} days .* at most {MAX_WARMUP_LOOKBACK_DAYS}\b"):
        resolve_deploy_strategy_params("sma_crossover", _SYMBOL, overrides)


@pytest.mark.parametrize("resolution_minutes", [391, 480, 1440])
def test_deploy_refuses_a_bar_longer_than_a_regular_session(resolution_minutes: int) -> None:
    """#2841: SMA on such a bar was accepted and sealed its default seven days.

    The bot then started unwarmed, or on a 1440-minute bar never decided at
    all: the days such a bar needs cannot be counted.
    """
    with pytest.raises(
        ValueError,
        match=(
            rf"Invalid strategy parameters: A {resolution_minutes}-minute bar is longer than a regular trading "
            r"session, so these periods cannot be warmed up from history\. Use a shorter bar\."
        ),
    ):
        resolve_deploy_strategy_params("sma_crossover", _SYMBOL, {"resolution_minutes": resolution_minutes})


def test_deploy_sizes_the_history_a_bar_one_session_long_needs() -> None:
    """A regular session holds one whole 390-minute bar, so its lookback is counted, and refused for its length.

    SMA's default periods need 60 days at that bar.
    """
    with pytest.raises(ValueError, match=rf"need 60 days .* at most {MAX_WARMUP_LOOKBACK_DAYS}\b"):
        resolve_deploy_strategy_params("sma_crossover", _SYMBOL, {"resolution_minutes": 390})


@pytest.mark.parametrize("strategy_key", sorted(_STRATEGY_REGISTRY))
def test_deploy_accepts_every_registered_strategy_at_its_own_defaults(strategy_key: str) -> None:
    """#2841: neither lookback refusal may refuse the periods a strategy is registered with."""
    assert resolve_deploy_strategy_params(strategy_key, _SYMBOL, {}).diverges_from_defaults == ()
