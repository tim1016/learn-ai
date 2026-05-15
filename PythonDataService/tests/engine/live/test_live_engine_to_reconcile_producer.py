"""Producer-consumer CI test: LiveEngine artifacts feed reconcile cleanly.

The dry-run gate (Tue 2026-05-19) is the real integration test; this is the
cheap CI version that proves the LiveEngine -> reconcile schema contract
without needing IB Gateway. Spec reference:
docs/superpowers/specs/2026-05-15-spy-ema-paper-dry-run-design.md §5.2.

Closes the §11 Phase 10 prereq "end-to-end producer test
(LiveEngine -> reconcile)" from ibkr-integration-authority.md.

What is tested here that the unit tests do NOT cover:

  * LiveEngine.run() with HydratePolicy.DISABLED produces an
    indicator_state_hydration.json at <run_dir>/indicator_state_hydration.json
    whose JSON structure is a valid HydrationReceipt (accepted=False,
    failure_reason="disabled_by_operator").

  * reconcile.write_day_report() reads that file and includes its SHA-256 in
    day-N.hashes.json under the key "indicator_state_hydration.json".

  * The committed Markdown day-N.md embeds the same SHA-256 in the
    artifact_hashes YAML block.

The test does NOT try to produce non-empty decisions.parquet from a live run
(the strategy is in warmup after only 2 consolidated bars) — instead it
writes a minimal synthetic decisions + executions + qc-indicators fixture
that satisfies reconcile's schema, then calls write_day_report. The contract
being tested is the artifact-schema handshake between LiveEngine and
reconcile, specifically the hydration receipt path.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.live.config import LiveConfig
from app.engine.live.indicator_state import HydratePolicy, HydrationReceipt
from app.engine.live.live_engine import LiveEngine
from app.engine.live.reconcile import write_day_report
from app.engine.strategy.algorithms.spy_ema_crossover import SpyEmaCrossoverAlgorithm
from tests.engine.live.fixtures.fake_broker import FakeBroker

_ET = ZoneInfo("America/New_York")

# A single 15-min aligned bar timestamp to use as the synthetic decision row.
_BAR_CLOSE_MS = int(pd.Timestamp(2026, 5, 18, 19, 15, tz="UTC").value // 1_000_000)  # 14:15 ET = 19:15 UTC


async def _two_bar_source() -> None:
    """Yield two 1-min SPY bars so last_bar is non-None and finally fires."""
    t0 = datetime(2026, 5, 18, 14, 0, tzinfo=_ET)
    for i in range(2):
        from datetime import timedelta

        t = t0 + timedelta(minutes=i)
        yield TradeBar(
            symbol="SPY",
            time=t,
            end_time=t + timedelta(minutes=1),
            open=Decimal("400"),
            high=Decimal("400"),
            low=Decimal("400"),
            close=Decimal("400"),
            volume=Decimal("0"),
        )


def _write_synthetic_run_inputs(run_dir: Path, qc_dir: Path) -> None:
    """Write minimal valid parquets and CSV so write_day_report has something to join.

    One HOLD bar on both sides — no fill, no halt. The synthetic fixture only
    needs to be schema-valid; the content is irrelevant because the producer
    test is specifically about the hydration receipt round-trip, not the
    cross-engine classification logic (that is covered by test_reconcile.py).
    """
    decisions = pd.DataFrame(
        [
            {
                "bar_close_ms": _BAR_CLOSE_MS,
                "ema5": 400.0,
                "ema10": 400.0,
                "rsi": 55.0,
                "signal": "HOLD",
                "intended_price": 400.0,
            }
        ]
    )
    executions = pd.DataFrame(
        columns=[
            "ts_ms",
            "exec_id",
            "perm_id",
            "client_order_id",
            "account_id",
            "symbol",
            "fill_quantity",
            "fill_price",
            "fee",
        ]
    )
    qc_indicators = pd.DataFrame(
        [
            {
                "bar_close_ms": _BAR_CLOSE_MS,
                "ema5": 400.05,
                "ema10": 400.02,
                "rsi": 55.5,
                "signal": "HOLD",
            }
        ]
    )

    run_dir.mkdir(parents=True, exist_ok=True)
    qc_dir.mkdir(parents=True, exist_ok=True)
    decisions.to_parquet(run_dir / "decisions.parquet", index=False)
    executions.to_parquet(run_dir / "executions.parquet", index=False)
    qc_indicators.to_csv(qc_dir / "indicators.csv", index=False)


@pytest.mark.asyncio
async def test_live_engine_produces_hydration_receipt_reconcile_consumes_it(
    tmp_path: Path,
) -> None:
    """End-to-end: LiveEngine writes the hydration receipt -> reconcile
    round-trips its SHA through hash manifest and Markdown.

    Contract assertions:
      1. LiveEngine.run() writes indicator_state_hydration.json at <run_dir>.
      2. The receipt's accepted=False and failure_reason='disabled_by_operator'
         (HydratePolicy.DISABLED was supplied — no prior sidecar on first day).
      3. write_day_report writes day-0.hashes.json with the correct SHA-256
         under key 'indicator_state_hydration.json'.
      4. day-0.md embeds the same SHA-256 in its artifact_hashes YAML block.
    """
    artifacts_root = tmp_path / "artifacts"
    run_dir = artifacts_root / "live_runs" / "producer_test"
    run_dir.mkdir(parents=True)
    qc_dir = tmp_path / "qc" / "2026-05-18"
    docs_dir = tmp_path / "docs-out"

    # --- Phase 1: Drive LiveEngine to produce the hydration receipt. ---
    strat = SpyEmaCrossoverAlgorithm()
    engine = LiveEngine(
        client=None,
        broker=FakeBroker(),
        output_dir=run_dir,
        artifacts_root=artifacts_root,
        hydrate_policy=HydratePolicy.DISABLED,
        session_start_ms=int(datetime(2026, 5, 18, 9, 30, tzinfo=_ET).timestamp() * 1000),
        config=LiveConfig(symbol="SPY", force_flat_at=None),
        code_sha="producer-test",
        strategy_spec_sha="producer-test",
    )

    await engine.run(strat, bars=_two_bar_source())

    # --- Assertion 1: receipt exists. ---
    receipt_path = run_dir / "indicator_state_hydration.json"
    assert receipt_path.exists(), "LiveEngine did not write the hydration receipt"

    # --- Assertion 2: receipt shape is valid and matches DISABLED policy. ---
    receipt_bytes = receipt_path.read_bytes()
    receipt_obj = HydrationReceipt.model_validate_json(receipt_bytes)
    assert receipt_obj.accepted is False
    assert receipt_obj.validation.failure_reason == "disabled_by_operator"
    assert receipt_obj.policy == HydratePolicy.DISABLED

    # Compute the expected SHA-256 from the bytes LiveEngine wrote.
    expected_sha = hashlib.sha256(receipt_bytes).hexdigest()

    # --- Phase 2: Wire up synthetic reconcile inputs and run write_day_report. ---
    # decisions.parquet / executions.parquet are NOT written by LiveEngine in
    # warmup (no consolidated bars reach the decision writer), so we write
    # minimal synthetic parquets so reconcile has a valid schema to join on.
    # The contract we care about is the hydration receipt round-trip, not the
    # cross-engine classification of these rows.
    _write_synthetic_run_inputs(run_dir, qc_dir)

    paths = write_day_report(
        run_dir=run_dir,
        qc_dir=qc_dir,
        docs_dir=docs_dir,
        run_label="producer-test-2026-05-18",
        day_n=0,
        day_date=date(2026, 5, 18),
    )

    # --- Assertion 3: hash manifest contains the hydration receipt SHA. ---
    assert paths.hashes.exists(), f"day-0.hashes.json not written at {paths.hashes}"
    hashes = json.loads(paths.hashes.read_text(encoding="utf-8"))
    assert "indicator_state_hydration.json" in hashes, (
        f"hash manifest does not include indicator_state_hydration.json; keys={sorted(hashes.keys())}"
    )
    assert hashes["indicator_state_hydration.json"] == expected_sha, (
        f"SHA mismatch: manifest={hashes['indicator_state_hydration.json']!r} expected={expected_sha!r}"
    )

    # --- Assertion 4: Markdown embeds the SHA. ---
    assert paths.md.exists(), f"day-0.md not written at {paths.md}"
    md_text = paths.md.read_text(encoding="utf-8")
    assert expected_sha in md_text, "day-0.md artifact_hashes block does not embed the hydration receipt SHA-256"
