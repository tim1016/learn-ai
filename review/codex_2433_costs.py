"""Offline characterization of the public request's resolved fee contract."""
from decimal import Decimal

from app.engine.execution.execution_config import ExecutionConfig
from app.schemas.engine_backtest import EngineBacktestRequest
from app.services.engine_backtest_service import _build_backtest_engine


def test_paired_profile_accepts_but_does_not_use_editable_flat_commission():
    """Preserve the distinction between submitted flat cost and pinned fees."""
    ordinary = EngineBacktestRequest(
        strategy_name="ema-crossover", params={"symbol": "SPY"},
        commission_per_order=99, save_study=False,
    )
    payload = ordinary.model_dump()
    payload["data_policy"]["adjusted"] = False
    payload["requested_engine"] = "both"
    payload["compatibility_profile"] = "us-equity-raw-ibkr-v1"
    paired = EngineBacktestRequest.model_validate(payload)
    assert paired.commission_per_order == 99
    config = ExecutionConfig(commission_per_order=Decimal(99))
    # The builder does not touch this source; no engine or data provider runs.
    source = object()
    flat = _build_backtest_engine(reader=source, execution_config=config, request=ordinary)
    fixed = _build_backtest_engine(reader=source, execution_config=config, request=paired)
    assert flat.fill_model.compute_fee(quantity=100, fill_price=Decimal(100)) == Decimal(99)
    assert fixed.fill_model.compute_fee(quantity=100, fill_price=Decimal(100)) == Decimal(1)
    assert paired.model_dump()["commission_per_order"] == 99
