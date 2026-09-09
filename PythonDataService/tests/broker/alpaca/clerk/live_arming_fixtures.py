"""One live account, one sealed instance, one shadow receipt, one activation proof.

The evidence the arming ceremony observes (ADR 0059 D3 R8), laid down with the
repository's own writers so a test exercises the real readers instead of a mock
of them.

Not a conftest: these builders are imported by name from
``tests/broker/alpaca/clerk/``, ``tests/scripts/`` and ``tests/services/``,
which share no conftest -- the same reason ``live_envelope_fixtures`` is a plain
module. They extend that module rather than restating it, so every slice-6 test
and every slice-5 test describe the same live account.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.account_authority import shadow_account_id_for_live_account
from app.broker.alpaca.clerk.shadow_activation import ShadowActivationRecord, ShadowActivationStore
from app.broker.alpaca.clerk.shadow_receipt import (
    ShadowReceipt,
    ShadowReceiptSession,
    ShadowReceiptStore,
)
from app.broker.alpaca.config import AlpacaSettings
from app.schemas.signal_program_seal import (
    ConfiguredSignalProgramSeal,
    ExitEligibilityContract,
    NumericalProvenanceContract,
    ResolvedSignalParameter,
    SealedBotProgram,
    SignalBarIntegrityContract,
    SignalClockContract,
    SignalDataContract,
    SignalSeriesContract,
    seal_bot_program,
)
from app.services.bot_binding_repository import (
    BrokerBotBinding,
    alpaca_v1_action_plan,
    live_state_binding_repository,
)
from app.services.session_authority import et_minute_of_day_ms
from tests.broker.alpaca.clerk.live_envelope_fixtures import (
    LIVE_ACCT,
    SHADOW_ACCT,
    TEST_ENVELOPE_VALUES,
)

ARMING_SID = "ema-shadow-1"
# Friday 2026-09-11, 10:00 ET -- a full NYSE session, so the arming spends
# exactly one session and the weekend that follows spends none.
ARMED_AT_MS = et_minute_of_day_ms(date(2026, 9, 11), 10 * 60)
_ONE_DAY_MS = 86_400_000


def live_settings(**overrides: Any) -> AlpacaSettings:
    """Live settings whose envelope is exactly ``TEST_ENVELOPE_VALUES``.

    Constructed with explicit keyword arguments, which outrank the process
    environment and ``.env`` in pydantic-settings, so the values are the test's
    and never the developer's.
    """
    values: dict[str, Any] = {
        "api_key_id": "k",
        "api_secret_key": "s",
        "mode": "live",
        "live_loss_fraction": TEST_ENVELOPE_VALUES.loss_fraction,
        "live_loss_usd": TEST_ENVELOPE_VALUES.loss_usd,
        "live_shadow_sessions": TEST_ENVELOPE_VALUES.shadow_sessions,
        "live_arming_max_sessions": TEST_ENVELOPE_VALUES.arming_max_sessions,
        "live_xh_entry_bps": TEST_ENVELOPE_VALUES.xh_entry_bps,
        "live_xh_exit_bps": TEST_ENVELOPE_VALUES.xh_exit_bps,
    }
    values.update(overrides)
    return AlpacaSettings(**values)


def paper_settings() -> AlpacaSettings:
    return AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper")


def activate_shadow_fence(
    artifacts_root: Path,
    *,
    live_account_id: str = LIVE_ACCT,
    activated_at_ms: int = ARMED_AT_MS,
) -> ShadowActivationRecord:
    """The shadow activation proof alone, without opening a custody database.

    ``activate_shadow_clerk_authority`` would also create the SQLite authority
    and hold its execution lease; the ceremony reads only the fence, so this
    writes only the fence.
    """
    record = ShadowActivationRecord.create(
        account_id=shadow_account_id_for_live_account(live_account_id),
        authority_generation=1,
        db_identity_token=f"arming-fixture-{live_account_id}",
        activated_at_ms=activated_at_ms,
    )
    ShadowActivationStore(artifacts_root).append(record)
    return record


def sealed_program(
    *,
    strategy_instance_id: str = ARMING_SID,
    sealed_account_id: str = SHADOW_ACCT,
    quantity: int = 1,
) -> SealedBotProgram:
    """A minimal but genuine v2 program seal, self-hashed by ``seal_bot_program``."""
    configured = ConfiguredSignalProgramSeal(
        program_key="ema_crossover_signal",
        program_version="ema-crossover-signal/v1",
        protocol_version="signal-session-protocol/v1",
        parameter_schema_version="ema-crossover-signal-params/v1",
        golden_trace_root="a" * 64,
        parameters={
            "fast_period": ResolvedSignalParameter(value=12, unit="bars", origin="registered_default"),
        },
        parameters_match_validated_settings=True,
        data=SignalDataContract(
            provider="polygon",
            symbol="SPY",
            base_timeframe_ms=60_000,
            decision_timeframe_ms=60_000,
        ),
        clock=SignalClockContract(use_rth=True, warmup_lookback_days=5),
        signals=(SignalSeriesContract(name="fast", indicator="ema", field="close", period=12, warmup_bars=12),),
        decision_streams=("ENTER", "EXIT"),
        bar_integrity=SignalBarIntegrityContract(),
        exit_eligibility=ExitEligibilityContract(countdown_decision_clocks=5, countdown_state_persistable=False),
        numerical_provenance=NumericalProvenanceContract(
            formula="test formula",
            reference="test reference",
            canonical_implementation="test canonical implementation",
            validated_against="test validated against",
            equivalence_level="bit_exact",
        ),
    )
    return seal_bot_program(
        strategy_instance_id=strategy_instance_id,
        configured_signal=configured,
        configured_signal_hash=configured.semantic_hash(),
        broker="alpaca",
        sealed_account_id=sealed_account_id,
        mode="trade",
        action_plan={"on_enter": [], "on_exit": []},
        quantity=quantity,
        carryover_policy="FORBID",
        validation_event_id="event-1",
        validation_snapshot_sha256="d" * 64,
        sealed_at_ms=1_000,
    )


def record_sealed_binding(
    live_state_root: Path,
    *,
    strategy_instance_id: str = ARMING_SID,
    sealed_account_id: str = SHADOW_ACCT,
    quantity: int = 1,
) -> SealedBotProgram:
    """Put one readable, sealed runner binding on disk and return its seal.

    ``quantity`` is a parameter because R2 binds arming to the *whole* sealed
    program: changing it is how a test proves a size change disarms.
    """
    seal = sealed_program(
        strategy_instance_id=strategy_instance_id,
        sealed_account_id=sealed_account_id,
        quantity=quantity,
    )
    live_state_binding_repository(live_state_root).record_launch(
        BrokerBotBinding(
            strategy_instance_id=strategy_instance_id,
            broker="alpaca",
            symbol="SPY",
            mode="trade",
            quantity=quantity,
            action_plan=alpaca_v1_action_plan("SPY"),
            sealed_program=seal,
            sealed_account_id=sealed_account_id,
            run_id=f"{strategy_instance_id}-run-1",
            created_at_ms=1_757_000_000_000,
        ),
        launch_reason="deploy",
    )
    return seal


def seal_receipt(
    artifacts_root: Path,
    *,
    configured_signal_hash: str,
    live_account_id: str = LIVE_ACCT,
    strategy_instance_id: str = ARMING_SID,
    sessions: int = 1,
    written_at_ms: int = ARMED_AT_MS - _ONE_DAY_MS,
) -> ShadowReceipt:
    """One shadow receipt that is current for this instance's configured signal."""
    receipt = ShadowReceipt.create(
        live_account_id=live_account_id,
        strategy_instance_id=strategy_instance_id,
        configured_signal_hash=configured_signal_hash,
        twin_account_id="PA-TWIN-ARM",
        twin_strategy_instance_id=f"{strategy_instance_id}-twin",
        required_sessions=sessions,
        sessions=tuple(
            ShadowReceiptSession(
                session_open_ms=written_at_ms - _ONE_DAY_MS * (index + 1),
                shadow_run_id=f"{strategy_instance_id}-run-1",
                reconciliation_sha256="e" * 64,
            )
            for index in range(sessions)
        ),
        written_at_ms=written_at_ms,
    )
    ShadowReceiptStore(artifacts_root).append(receipt)
    return receipt


def arming_ready(
    artifacts_root: Path,
    live_state_root: Path,
    *,
    strategy_instance_id: str = ARMING_SID,
) -> SealedBotProgram:
    """Every input ``observe_arming_inputs`` needs, on disk, for one instance."""
    if not ShadowActivationStore(artifacts_root).account_ids():
        activate_shadow_fence(artifacts_root)
    seal = record_sealed_binding(live_state_root, strategy_instance_id=strategy_instance_id)
    seal_receipt(
        artifacts_root,
        configured_signal_hash=seal.configured_signal_hash,
        strategy_instance_id=strategy_instance_id,
        sessions=TEST_ENVELOPE_VALUES.shadow_sessions,
    )
    return seal


__all__ = [
    "ARMED_AT_MS",
    "ARMING_SID",
    "activate_shadow_fence",
    "arming_ready",
    "live_settings",
    "paper_settings",
    "record_sealed_binding",
    "seal_receipt",
    "sealed_program",
]
