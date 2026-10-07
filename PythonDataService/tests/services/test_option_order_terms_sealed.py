"""An option bot's four order terms are sealed with it, and a bot in shares is written as it always was (#2913).

The terms ride the binding, the instance record and the seal beside the
bot's plan. Each of those is hashed or stored, so a bot with none must dump
and hash to the byte as it did before the terms existed. The two hashes
pinned here were computed on the commit before the field was added.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.broker.alpaca.clerk.option_effects import OptionEffectRefusal, OptionPlanRefused
from app.engine.live.identity import strategy_instance_artifact_dir
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.schemas.option_order_terms import SealedOptionOrderTerms
from app.schemas.signal_program_seal import SealedBotProgram
from app.services import bot_option_run
from app.services.bot_binding_repository import (
    BotBindingRepository,
    BrokerBotBinding,
    StrategyInstanceRecord,
    alpaca_v1_action_plan,
)
from app.services.bot_carryover import configuration_hash, immutable_configuration_payload
from app.services.bot_runner_errors import (
    OPTION_BOT_REGULAR_HOURS_ONLY,
    OPTION_ORDER_TERMS_NOT_APPLICABLE,
    OPTION_ORDER_TERMS_REQUIRED,
    OptionStartRefusedError,
    require_option_start_configuration,
)
from app.services.signal_program_admission import _seal_checks, build_start_program_seal
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.schemas.test_signal_program_seal import _sealed_bot_program
from tests.services.bot_runner._option_floor import STARTING_TERMS, one_call
from tests.services.test_signal_program_admission import _binding as _program_binding
from tests.services.test_signal_program_admission import _validation

# A stock bot's configuration hash and its seal's hash, on the commit before the terms existed.
_STOCK_CONFIGURATION_HASH = "2519cb566e1ce75c15ebd2f589d5a854cf927a1072306463de20b0fe57e67130"
_STOCK_SEAL_HASH = "1144baa51c6e0337d6d2f002dcc7d4ae856d0f2203c386dbef59f697c0088764"

_OWN = SealedOptionOrderTerms(
    gap_limit_percent=Decimal("12.5"), entry_wait_seconds=7, cost_limit_percent=Decimal(4), expiry_close_lead_minutes=90
)


def _binding(*, option: bool, terms: SealedOptionOrderTerms | None = None) -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id="pinned-bot-1",
        strategy_key="ema_crossover_signal",
        broker="alpaca",
        symbol="SPY",
        mode="trade",
        quantity=3,
        action_plan=one_call() if option else alpaca_v1_action_plan("SPY"),
        option_order_terms=terms,
        strategy_params={"hold_bars": 5, "symbol": "SPY"},
        exit_terms=DEPLOY_EXIT_TERMS,
        sealed_account_id="PA-TEST",
        run_id="run-pinned-1",
        created_at_ms=1_787_356_800_000,
    )


# ── a bot in shares ─────────────────────────────────────────────────────


def test_a_stock_bots_binding_hashes_and_dumps_as_it_did_before_the_terms_existed() -> None:
    binding = _binding(option=False)

    assert configuration_hash(binding) == _STOCK_CONFIGURATION_HASH
    assert "option_order_terms" not in immutable_configuration_payload(binding)
    assert "option_order_terms" not in binding.model_dump(mode="json")
    assert "option_order_terms" not in StrategyInstanceRecord.from_binding(binding).model_dump(mode="json")


def test_a_stock_bots_seal_hashes_and_is_written_as_it_was_before_the_terms_existed() -> None:
    seal = _sealed_bot_program(action_plan=alpaca_v1_action_plan("SPY"))

    assert seal.bot_configuration_hash == _STOCK_SEAL_HASH
    assert "option_order_terms" not in json.loads(seal.model_dump_json())


def test_a_bot_in_shares_cannot_carry_option_order_terms() -> None:
    with pytest.raises(ValidationError, match="trades shares"):
        _binding(option=False, terms=_OWN)


# ── a bot in option contracts ───────────────────────────────────────────


def test_the_terms_an_option_bot_was_deployed_with_are_part_of_its_configuration() -> None:
    """Another term is another configuration: the Clerk keeps the bot under this hash."""
    sealed = _binding(option=True, terms=_OWN)
    one_second_longer = _binding(option=True, terms=_OWN.model_copy(update={"entry_wait_seconds": 8}))

    assert immutable_configuration_payload(sealed)["option_order_terms"] == {
        "gap_limit_percent": "12.5", "entry_wait_seconds": 7, "cost_limit_percent": "4", "expiry_close_lead_minutes": 90,
    }
    assert configuration_hash(sealed) != configuration_hash(one_second_longer)
    assert configuration_hash(sealed) != configuration_hash(_binding(option=True))


def test_one_choice_of_a_percent_has_one_hash_however_it_was_typed() -> None:
    typed = SealedOptionOrderTerms.model_validate(
        {"gap_limit_percent": "12.500", "entry_wait_seconds": 7, "cost_limit_percent": "4.0", "expiry_close_lead_minutes": 90}
    )

    assert typed == _OWN
    assert configuration_hash(_binding(option=True, terms=typed)) == configuration_hash(_binding(option=True, terms=_OWN))


@pytest.mark.parametrize(
    ("term", "value"),
    [
        ("gap_limit_percent", "0"),
        ("gap_limit_percent", "100.5"),
        ("cost_limit_percent", "-1"),
        ("entry_wait_seconds", 0),
        ("entry_wait_seconds", 31),
        ("expiry_close_lead_minutes", 391),
        ("entry_wait_seconds", None),
    ],
)
def test_a_term_outside_the_range_an_owner_is_offered_is_not_sealed(term: str, value: object) -> None:
    with pytest.raises(ValidationError):
        SealedOptionOrderTerms.model_validate({**_OWN.model_dump(mode="json"), term: value})


def test_every_one_of_the_four_terms_is_said() -> None:
    """Nothing is filled in for a term that was left out: what is sealed is what the owner set."""
    for term in ("gap_limit_percent", "entry_wait_seconds", "cost_limit_percent", "expiry_close_lead_minutes"):
        said = _OWN.model_dump(mode="json")
        del said[term]
        with pytest.raises(ValidationError):
            SealedOptionOrderTerms.model_validate(said)


def test_an_option_bots_terms_come_back_from_disk_with_the_bot(tmp_path: Path) -> None:
    binding = _binding(option=True, terms=_OWN)
    repository = BotBindingRepository(
        tmp_path, instance_dir_for=lambda sid: strategy_instance_artifact_dir(tmp_path, "live_state", sid)
    )

    repository.record_launch(binding, launch_reason="deploy")
    read = repository.read(binding.strategy_instance_id)

    assert read is not None
    assert read.option_order_terms == _OWN
    assert configuration_hash(read) == configuration_hash(binding)


def test_an_option_bots_terms_are_under_its_seal() -> None:
    seal = _sealed_bot_program(action_plan=one_call(), option_order_terms=_OWN)
    written = json.loads(seal.model_dump_json())

    assert written["option_order_terms"]["entry_wait_seconds"] == 7
    assert seal.bot_configuration_hash != _sealed_bot_program(action_plan=one_call()).bot_configuration_hash
    assert SealedBotProgram.model_validate(written) == seal
    written["option_order_terms"]["entry_wait_seconds"] = 30
    with pytest.raises(ValidationError, match="bot configuration hash"):
        SealedBotProgram.model_validate(written)
    del written["option_order_terms"]
    with pytest.raises(ValidationError, match="bot configuration hash"):
        SealedBotProgram.model_validate(written)


# ── the runner ──────────────────────────────────────────────────────────


def test_the_runner_reads_a_bots_order_terms_from_its_binding() -> None:
    terms = bot_option_run.option_order_terms(_binding(option=True, terms=_OWN))

    assert (terms.leg_gap_limit_percent, terms.cost_limit_percent) == (Decimal("12.5"), Decimal(4))
    assert (terms.close.entry_wait_ms, terms.close.expiry_close_lead_ms) == (7_000, 90 * 60_000)
    # The two times an owner does not set stay where the deadlines start them.
    started = bot_option_run.option_order_terms(_binding(option=True, terms=STARTING_TERMS)).close
    assert (terms.close.cancel_allowance_ms, terms.close.entry_margin_ms, terms.close.sold_leg_close_lead_ms) == (
        started.cancel_allowance_ms, started.entry_margin_ms, started.sold_leg_close_lead_ms,
    )


def test_the_runner_assumes_no_terms_for_a_bot_that_was_sealed_with_none() -> None:
    with pytest.raises(OptionPlanRefused) as refused:
        bot_option_run.option_order_terms(_binding(option=True))

    assert refused.value.reason is OptionEffectRefusal.ORDER_TERMS_NOT_SEALED


# ── where every bot starts ──────────────────────────────────────────────


def test_an_option_bot_starts_only_in_regular_hours_and_with_its_terms() -> None:
    """The runner's own rule, whatever route asked: no extended hours, and no start without the four terms."""
    with pytest.raises(OptionStartRefusedError) as extended:
        require_option_start_configuration(action_plan=one_call(), use_rth=False, option_order_terms=_OWN)
    with pytest.raises(OptionStartRefusedError) as no_terms:
        require_option_start_configuration(action_plan=one_call(), use_rth=True, option_order_terms=None)

    assert extended.value.reason_code == OPTION_BOT_REGULAR_HOURS_ONLY
    assert no_terms.value.reason_code == OPTION_ORDER_TERMS_REQUIRED
    require_option_start_configuration(action_plan=one_call(), use_rth=True, option_order_terms=_OWN)


def test_a_bot_in_shares_starts_as_it_always_did_and_never_with_option_terms() -> None:
    shares = alpaca_v1_action_plan("SPY")

    require_option_start_configuration(action_plan=shares, use_rth=True, option_order_terms=None)
    require_option_start_configuration(action_plan=shares, use_rth=False, option_order_terms=None)
    with pytest.raises(OptionStartRefusedError) as refused:
        require_option_start_configuration(action_plan=shares, use_rth=True, option_order_terms=_OWN)
    assert refused.value.reason_code == OPTION_ORDER_TERMS_NOT_APPLICABLE


def test_a_start_seals_the_terms_its_binding_carries() -> None:
    """The seal a first Start authors is made from the binding: its terms go under the seal's hash with its plan."""
    binding = _program_binding(action_plan=one_call(), option_order_terms=_OWN)
    in_shares = _program_binding()

    sealed = build_start_program_seal(binding, _validation(), parameter_origins=binding.strategy_param_origins)
    plain = build_start_program_seal(in_shares, _validation(), parameter_origins=in_shares.strategy_param_origins)

    assert sealed is not None and plain is not None
    assert sealed.option_order_terms == _OWN and plain.option_order_terms is None


def test_a_seal_made_under_other_order_terms_than_the_bot_is_bound_to_does_not_prove_its_build() -> None:
    """The seal's terms are what the bot was deployed under and the binding's are what its runner reads. The
    two are compared, and a difference is said by name."""
    binding = _program_binding(action_plan=one_call(), option_order_terms=_OWN)
    in_shares = _program_binding()
    contract = _STRATEGY_REGISTRY[binding.strategy_key].signal_program_contract
    assert contract is not None

    def what_fails(bound: BrokerBotBinding) -> str | None:
        sealed = build_start_program_seal(binding, _validation(), parameter_origins=binding.strategy_param_origins)
        assert sealed is not None
        return next((check.name for check in _seal_checks(bound, sealed, contract) if not check.holds), None)

    other_terms = binding.model_copy(update={"option_order_terms": _OWN.model_copy(update={"entry_wait_seconds": 21})})
    plain = build_start_program_seal(in_shares, _validation(), parameter_origins=in_shares.strategy_param_origins)
    assert plain is not None

    assert what_fails(binding) is None
    assert what_fails(other_terms) == "option_order_terms"
    assert all(check.holds for check in _seal_checks(in_shares, plain, contract)), "a bot in shares has none on either"


@pytest.mark.parametrize(
    ("term", "not_a_whole_number"),
    [("entry_wait_seconds", True), ("expiry_close_lead_minutes", False), ("entry_wait_seconds", 20.0), ("entry_wait_seconds", "20")],
)
def test_a_boolean_or_a_number_written_another_way_is_not_sealed_as_a_whole_number_term(
    term: str, not_a_whole_number: object
) -> None:
    """``true`` is not a wait of one second and ``false`` is not a lead of no minutes. A JSON ``20`` is twenty."""
    said = {"gap_limit_percent": "8", "entry_wait_seconds": 20, "cost_limit_percent": "10", "expiry_close_lead_minutes": 45}

    assert SealedOptionOrderTerms.model_validate(said).entry_wait_seconds == 20
    assert SealedOptionOrderTerms.model_validate_json(json.dumps(said)).expiry_close_lead_minutes == 45
    with pytest.raises(ValidationError):
        SealedOptionOrderTerms.model_validate({**said, term: not_a_whole_number})
