"""Tests for the hydrate/write flow at the LiveContext layer.

Covers the six-row validation ladder under all three policy modes plus
the maybe_write skip semantics (None payload, newer-check).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.engine.live.indicator_state import (
    HydratePolicy,
    HydrationReceipt,
    IndicatorStateEnvelope,
    IndicatorStateHydrationError,
)
from app.engine.live.live_context import LiveContext


def _fake_strategy_with_payload(payload: dict | None, validate_ok: bool = True) -> MagicMock:
    """Build a MagicMock standing in for a strategy with persistence hooks."""
    from app.engine.live.indicator_state import ValidationResult

    s = MagicMock()
    s.STRATEGY_KEY = "spy_ema_crossover"
    s.CONSOLIDATOR_PERIOD_MIN = 15
    # Make strategy.ctx.symbols return ["SPY"] for the hydrate() function's symbol lookup.
    s.ctx = MagicMock()
    s.ctx.symbols = ["SPY"]
    s._symbol_name = "SPY"
    s.report_state_for_persistence.return_value = payload
    s.validate_state_payload.return_value = (
        ValidationResult.all_passed()
        if validate_ok
        else ValidationResult.failed("payload_mismatch", payload_shape_ok=False)
    )
    s.restore_state_from_persistence.return_value = None
    return s


def _valid_envelope_dict(last_bar_ms: int = 1779133500000) -> dict:
    """Envelope for Mon 2026-05-18 force-flat at 15:45 ET (= 19:45 UTC = 1779133500000 ms)."""
    return {
        "schema_version": 1,
        "strategy_key": "spy_ema_crossover",
        "symbol": "SPY",
        "consolidator_period_min": 15,
        "last_consolidated_bar_end_ms": last_bar_ms,
        "captured_at_ms": last_bar_ms + 7000,
        "captured_reason": "force_flat",
        "code_sha": "abc",
        "strategy_spec_sha": "def",
        "payload": {
            "ema5": {
                "name": "EMA5",
                "period": 5,
                "samples": 18,
                "current_value": "412.34",
                "current_time_ms": last_bar_ms,
            },
            "ema10": {
                "name": "EMA10",
                "period": 10,
                "samples": 18,
                "current_value": "411.23",
                "current_time_ms": last_bar_ms,
            },
            "rsi14": {
                "name": "RSI14",
                "period": 14,
                "samples": 18,
                "current_value": "58.42",
                "current_time_ms": last_bar_ms,
            },
            "_prev_ema5_above_ema10": True,
            "lifecycle": {"position_qty": 0, "pending_orders_count": 0, "open_insights": 0},
        },
    }


def _make_ctx(tmp_path: Path, policy: HydratePolicy = HydratePolicy.REQUIRE) -> LiveContext:
    """Construct a LiveContext rigged for hydrate testing."""
    portfolio = MagicMock()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()
    return LiveContext(
        portfolio=portfolio,
        hydrate_policy=policy,
        run_dir=run_dir,
        artifacts_root=artifacts_root,
        # Tue 2026-05-19 09:30 ET = 13:30 UTC = 1779197400000 ms (session start).
        session_start_ms=1779197400000,
    )


# ---- happy path ----


def test_require_happy_path_restores_and_writes_accepted_receipt(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, HydratePolicy.REQUIRE)
    from app.engine.live.indicator_state import IndicatorStateRepo, stable_global_path

    expected_prev_close_ms = 1779134400000  # Mon 2026-05-18 16:00 ET = 20:00 UTC ms
    env_dict = _valid_envelope_dict(last_bar_ms=expected_prev_close_ms)
    repo = IndicatorStateRepo(stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15))
    repo.write(IndicatorStateEnvelope.model_validate(env_dict))

    strat = _fake_strategy_with_payload(payload=env_dict["payload"])
    ctx.hydrate_indicator_state(strat)

    strat.restore_state_from_persistence.assert_called_once()
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.accepted is True


# ---- failure ladder under REQUIRE ----


def test_require_missing_raises_and_writes_missing_receipt(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, HydratePolicy.REQUIRE)
    strat = _fake_strategy_with_payload(payload=None)
    with pytest.raises(IndicatorStateHydrationError):
        ctx.hydrate_indicator_state(strat)
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.accepted is False
    assert receipt.validation.failure_reason == "missing"


def test_require_calendar_stale_raises(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, HydratePolicy.REQUIRE)
    from app.engine.live.indicator_state import IndicatorStateRepo, stable_global_path

    bad_old_close_ms = 1778270400000  # Fri 2026-05-08 16:00 ET = 20:00 UTC — too old (prior session was Mon 2026-05-18)
    env_dict = _valid_envelope_dict(last_bar_ms=bad_old_close_ms)
    repo = IndicatorStateRepo(stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15))
    repo.write(IndicatorStateEnvelope.model_validate(env_dict))
    strat = _fake_strategy_with_payload(payload=env_dict["payload"])
    with pytest.raises(IndicatorStateHydrationError):
        ctx.hydrate_indicator_state(strat)
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.validation.failure_reason == "calendar_stale"


def test_require_identity_mismatch_raises(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, HydratePolicy.REQUIRE)
    from app.engine.live.indicator_state import IndicatorStateRepo, stable_global_path

    env_dict = _valid_envelope_dict(last_bar_ms=1779134400000)
    env_dict["symbol"] = "QQQ"
    # Write to the QQQ-keyed path so the read finds it (since identity is checked AFTER read).
    # Actually: the read uses the runner's identity (SPY), and the envelope's symbol is QQQ.
    # The envelope is written via repo at SPY's stable path — that's how a wrong-symbol envelope ends up at the SPY path.
    repo = IndicatorStateRepo(stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15))
    repo.write(IndicatorStateEnvelope.model_validate(env_dict))
    strat = _fake_strategy_with_payload(payload=env_dict["payload"])
    with pytest.raises(IndicatorStateHydrationError):
        ctx.hydrate_indicator_state(strat)
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.validation.failure_reason == "identity_mismatch"


def test_require_payload_mismatch_raises(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, HydratePolicy.REQUIRE)
    from app.engine.live.indicator_state import IndicatorStateRepo, stable_global_path

    env_dict = _valid_envelope_dict(last_bar_ms=1779134400000)
    repo = IndicatorStateRepo(stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15))
    repo.write(IndicatorStateEnvelope.model_validate(env_dict))
    # Strategy's validate_state_payload rejects (e.g. spec drift).
    strat = _fake_strategy_with_payload(payload=env_dict["payload"], validate_ok=False)
    with pytest.raises(IndicatorStateHydrationError):
        ctx.hydrate_indicator_state(strat)
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.validation.failure_reason == "payload_mismatch"


def test_require_lifecycle_not_flat_raises(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, HydratePolicy.REQUIRE)
    from app.engine.live.indicator_state import IndicatorStateRepo, stable_global_path

    env_dict = _valid_envelope_dict(last_bar_ms=1779134400000)
    env_dict["payload"]["lifecycle"]["position_qty"] = 100
    repo = IndicatorStateRepo(stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15))
    repo.write(IndicatorStateEnvelope.model_validate(env_dict))
    strat = _fake_strategy_with_payload(payload=env_dict["payload"])
    with pytest.raises(IndicatorStateHydrationError):
        ctx.hydrate_indicator_state(strat)
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.validation.failure_reason == "lifecycle_not_flat"


def test_require_schema_mismatch_raises_on_corrupt_json(tmp_path: Path) -> None:
    """Malformed JSON on disk produces a schema_mismatch failure under REQUIRE."""
    ctx = _make_ctx(tmp_path, HydratePolicy.REQUIRE)
    from app.engine.live.indicator_state import stable_global_path

    path = stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json")
    strat = _fake_strategy_with_payload(payload=None)
    with pytest.raises(IndicatorStateHydrationError):
        ctx.hydrate_indicator_state(strat)
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.accepted is False
    assert receipt.validation.failure_reason == "schema_mismatch"


def test_require_indicators_unready_raises(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, HydratePolicy.REQUIRE)
    from app.engine.live.indicator_state import IndicatorStateRepo, stable_global_path

    env_dict = _valid_envelope_dict(last_bar_ms=1779134400000)
    env_dict["payload"]["ema5"]["samples"] = 2  # below period=5
    repo = IndicatorStateRepo(stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15))
    repo.write(IndicatorStateEnvelope.model_validate(env_dict))
    strat = _fake_strategy_with_payload(payload=env_dict["payload"])
    with pytest.raises(IndicatorStateHydrationError):
        ctx.hydrate_indicator_state(strat)
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.validation.failure_reason == "indicators_unready"


# ---- OPTIONAL policy ----


def test_optional_missing_cold_starts_and_writes_receipt(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, HydratePolicy.OPTIONAL)
    strat = _fake_strategy_with_payload(payload=None)
    ctx.hydrate_indicator_state(strat)  # no raise
    strat.restore_state_from_persistence.assert_not_called()
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.accepted is False
    assert receipt.validation.failure_reason == "missing"


def test_optional_calendar_stale_cold_starts(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, HydratePolicy.OPTIONAL)
    from app.engine.live.indicator_state import IndicatorStateRepo, stable_global_path

    env_dict = _valid_envelope_dict(last_bar_ms=1778270400000)
    repo = IndicatorStateRepo(stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15))
    repo.write(IndicatorStateEnvelope.model_validate(env_dict))
    strat = _fake_strategy_with_payload(payload=env_dict["payload"])
    ctx.hydrate_indicator_state(strat)
    strat.restore_state_from_persistence.assert_not_called()
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.validation.failure_reason == "calendar_stale"


# ---- DISABLED policy ----


def test_disabled_writes_receipt_without_reading_sidecar(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path, HydratePolicy.DISABLED)
    strat = _fake_strategy_with_payload(payload=None)
    ctx.hydrate_indicator_state(strat)
    strat.restore_state_from_persistence.assert_not_called()
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.policy == HydratePolicy.DISABLED
    assert receipt.validation.failure_reason == "disabled_by_operator"


# ---- maybe_write ----


def test_require_restore_failure_treated_as_schema_mismatch(tmp_path: Path) -> None:
    """If validate_state_payload passes but restore_state_from_persistence raises
    a nested-field KeyError, the ladder converts it to a schema_mismatch receipt."""
    ctx = _make_ctx(tmp_path, HydratePolicy.REQUIRE)
    from app.engine.live.indicator_state import IndicatorStateRepo, stable_global_path

    expected_prev_close_ms = 1779134400000  # Mon 2026-05-18 16:00 ET
    env_dict = _valid_envelope_dict(last_bar_ms=expected_prev_close_ms)
    repo = IndicatorStateRepo(stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15))
    repo.write(IndicatorStateEnvelope.model_validate(env_dict))

    strat = _fake_strategy_with_payload(payload=env_dict["payload"], validate_ok=True)
    # Make the fake strategy raise during restore (simulating a missing nested field).
    strat.restore_state_from_persistence.side_effect = KeyError("current_time_ms")

    with pytest.raises(IndicatorStateHydrationError):
        ctx.hydrate_indicator_state(strat)
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.accepted is False
    assert receipt.validation.failure_reason == "schema_mismatch"


def test_require_corrupted_samples_treated_as_indicators_unready(tmp_path: Path) -> None:
    """Malformed counters (samples='NaN') do not crash _indicators_ready;
    they surface as indicators_unready."""
    ctx = _make_ctx(tmp_path, HydratePolicy.REQUIRE)
    from app.engine.live.indicator_state import IndicatorStateRepo, stable_global_path

    expected_prev_close_ms = 1779134400000
    env_dict = _valid_envelope_dict(last_bar_ms=expected_prev_close_ms)
    env_dict["payload"]["ema5"]["samples"] = "NaN"  # corrupted counter
    repo = IndicatorStateRepo(stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15))
    repo.write(IndicatorStateEnvelope.model_validate(env_dict))

    strat = _fake_strategy_with_payload(payload=env_dict["payload"])
    with pytest.raises(IndicatorStateHydrationError):
        ctx.hydrate_indicator_state(strat)
    receipt = HydrationReceipt.model_validate_json((ctx.run_dir / "indicator_state_hydration.json").read_text())
    assert receipt.validation.failure_reason == "indicators_unready"


def test_maybe_write_skips_when_strategy_reports_none(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    strat = _fake_strategy_with_payload(payload=None)
    ctx.maybe_write_indicator_state(
        strat, reason="force_flat", code_sha="x", strategy_spec_sha="y", last_consolidated_bar_end_ms=1779134400000
    )
    from app.engine.live.indicator_state import stable_global_path

    global_path = stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15)
    assert not global_path.exists()


def test_maybe_write_force_flat_writes(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    payload = _valid_envelope_dict()["payload"]
    strat = _fake_strategy_with_payload(payload=payload)
    ctx.maybe_write_indicator_state(
        strat, reason="force_flat", code_sha="x", strategy_spec_sha="y", last_consolidated_bar_end_ms=1779134400000
    )
    from app.engine.live.indicator_state import stable_global_path

    global_path = stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15)
    assert global_path.exists()
    on_disk = IndicatorStateEnvelope.model_validate_json(global_path.read_text())
    assert on_disk.captured_reason == "force_flat"
    assert on_disk.last_consolidated_bar_end_ms == 1779134400000


def test_maybe_write_shutdown_refuses_to_overwrite_newer_force_flat(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    payload = _valid_envelope_dict()["payload"]
    strat = _fake_strategy_with_payload(payload=payload)
    # Existing force-flat write at Mon 2026-05-18 16:00 ET.
    ctx.maybe_write_indicator_state(
        strat, reason="force_flat", code_sha="x", strategy_spec_sha="y", last_consolidated_bar_end_ms=1779134400000
    )
    # Attempted shutdown write at Mon 2026-05-18 11:00 ET (older bar). Should refuse.
    older_bar_ms = 1779116400000  # Mon 2026-05-18 11:00 ET = 15:00 UTC ms
    ctx.maybe_write_indicator_state(
        strat, reason="shutdown", code_sha="x", strategy_spec_sha="y", last_consolidated_bar_end_ms=older_bar_ms
    )
    from app.engine.live.indicator_state import stable_global_path

    global_path = stable_global_path(ctx.artifacts_root, "spy_ema_crossover", "SPY", 15)
    on_disk = IndicatorStateEnvelope.model_validate_json(global_path.read_text())
    # Still the force-flat write — shutdown was refused.
    assert on_disk.captured_reason == "force_flat"
    assert on_disk.last_consolidated_bar_end_ms == 1779134400000
