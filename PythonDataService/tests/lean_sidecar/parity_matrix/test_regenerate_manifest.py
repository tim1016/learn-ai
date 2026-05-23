"""The cell manifest's `broker` block is the documented contract LEAN ran
under. After Task 6 it must say InteractiveBrokers / IBKR equity fee, not
the default brokerage."""

from __future__ import annotations

from pathlib import Path


def test_regen_manifest_broker_block_pins_interactive_brokers() -> None:
    src = (Path(__file__).resolve().parents[3] / "scripts" / "regenerate_cross_engine_study.py").read_text(
        encoding="utf-8"
    )
    # Must declare InteractiveBrokersBrokerage and the IBKR fee model name.
    assert '"brokerage_model": "InteractiveBrokersBrokerage"' in src
    assert '"fee_model": "InteractiveBrokersFeeModel"' in src
    # Default brokerage / zero-fee strings must NOT appear in the manifest block.
    assert '"brokerage_model": "DefaultBrokerageModel"' not in src
    assert '"fee_model": "ConstantFeeModel(0)"' not in src
