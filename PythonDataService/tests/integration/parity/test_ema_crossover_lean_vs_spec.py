"""LEAN ema_crossover ≡ spec spy_ema_crossover acceptance gate.

Runs both engines on the same SPY data window, persists each through .NET,
queries ``compareBacktestRuns`` over GraphQL, and asserts zero divergences in
the gating set from ``.claude/rules/numerical-rigor.md``:

  {DECISION_MISMATCH, DIRECTION_MISMATCH, QUANTITY_MISMATCH,
   FILL_PRICE_DRIFT, ORDER_TYPE_MISMATCH, PNL_DRIFT,
   FIXTURE_INSUFFICIENT}

``@pytest.mark.slow`` — excluded from default CI runs. Heavyweight: launches
the pinned LEAN container, reads SPY minute bars off disk, persists two
``StrategyExecution`` rows. Skip-guarded so the test no-ops gracefully when
the LEAN launcher, .NET backend, or LEAN data dump aren't available.

To run locally:

  podman compose up -d                # backend + python-service + postgres
  python PythonDataService/scripts/lean_sidecar_pin_image.py
  cd PythonDataService && \
    .venv/Scripts/python.exe -m uvicorn app.lean_sidecar.launcher.app:app \
      --host 0.0.0.0 --port 8090

  MSYS_NO_PATHCONV=1 podman exec polygon-data-service \
    python -m pytest /app/tests/integration/parity -v -m slow
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.engine.data.lean_format import LeanMinuteDataReader
from app.lean_sidecar.config import PINNED_LEAN_IMAGE_DIGEST
from app.services.lean_sidecar_service import TrustedRunRequest, run_trusted_sample
from app.services.spec_strategy_runner import run_spec_against_bars_and_persist

logger = logging.getLogger(__name__)

# Pinned reconciliation window. Update only with justification (LEAN image
# upgrade, fixture refresh, spec change) and regenerate the report in
# ``docs/references/reconciliations/``.
SYMBOL = "SPY"
WINDOW_START = date(2025, 1, 6)
WINDOW_END = date(2025, 1, 10)
STARTING_CASH = 100_000.0
STRATEGY_NAME = "ema_crossover"

GATING_CATEGORIES = frozenset(
    {
        "DECISION_MISMATCH",
        "DIRECTION_MISMATCH",
        "QUANTITY_MISMATCH",
        "FILL_PRICE_DRIFT",
        "ORDER_TYPE_MISMATCH",
        "PNL_DRIFT",
        "FIXTURE_INSUFFICIENT",
    }
)

BACKEND_URL = os.environ.get("BACKEND_URL", "http://backend:8080")
LEAN_DATA_ROOTS = [Path("/lean-cache"), Path("/lean-data")]
SPEC_FIXTURE_PATH = Path("/app/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json")
REPORT_OUTPUT_DIR = Path("/app/artifacts/parity-reports")


def _date_to_ms_utc(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


async def _require_backend_reachable() -> None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{BACKEND_URL}/health")
            response.raise_for_status()
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        pytest.skip(f"Backend not reachable at {BACKEND_URL}: {exc}")


async def _require_compare_resolver_in_schema() -> None:
    """Skip if the backend hasn't compiled `compareBacktestRuns` yet.

    The backend uses `dotnet watch run`; a stale NuGet restore or build error
    can leave it running an older compiled snapshot that doesn't expose this
    resolver. Without it the parity test fails at the comparison step with
    an opaque 400 instead of skipping cleanly.
    """
    introspect = {"query": 'query{__type(name:"Query"){fields{name}}}'}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(f"{BACKEND_URL}/graphql", json=introspect)
            response.raise_for_status()
        fields = response.json()["data"]["__type"]["fields"]
    except (httpx.HTTPError, KeyError, TypeError) as exc:
        pytest.skip(f"Could not introspect GraphQL schema: {exc}")
    if not any(f["name"] == "compareBacktestRuns" for f in fields):
        pytest.skip(
            "compareBacktestRuns missing from Query schema; backend likely has a stale build "
            "(check `podman logs my-backend` for NuGet/dotnet errors and rebuild)"
        )


async def _require_launcher_reachable() -> None:
    launcher_url = os.environ.get("LEAN_LAUNCHER_URL", "http://host.containers.internal:8090")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # The launcher exposes /health under the same auth model as the
            # actionable endpoints. We accept 200 or 401 (unauthorized) as
            # "reachable" — 401 means the process is up and just guarding the
            # endpoint; the test path itself supplies the token.
            response = await client.get(f"{launcher_url}/healthz")
            if response.status_code not in {200, 401}:
                pytest.skip(f"LEAN launcher returned HTTP {response.status_code} at {launcher_url}/healthz")
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        pytest.skip(f"LEAN launcher not reachable at {launcher_url}: {exc}")


def _require_lean_data_for_window() -> None:
    """Confirm at least the first trading day's SPY zip exists under either root."""
    reader = LeanMinuteDataReader(LEAN_DATA_ROOTS)
    dates_with_data = list(reader.iter_dates(SYMBOL, WINDOW_START, WINDOW_END))
    if not dates_with_data:
        pytest.skip(f"No LEAN SPY minute zips found under {LEAN_DATA_ROOTS} for window {WINDOW_START}..{WINDOW_END}")


def _require_pinned_lean_image() -> None:
    if PINNED_LEAN_IMAGE_DIGEST is None:
        pytest.skip("PINNED_LEAN_IMAGE_DIGEST not set; run scripts/lean_sidecar_pin_image.py first")


async def _query_compare_backtest_runs(
    left_id: int,
    right_id: int,
) -> dict:
    query = """
        query Compare($leftId: Int!, $rightId: Int!) {
          compareBacktestRuns(leftId: $leftId, rightId: $rightId) {
            left { id source strategyName totalTrades totalPnL finalEquity }
            right { id source strategyName totalTrades totalPnL finalEquity }
            guardrails { sameAlgorithm sameSymbol sameWindow sameParameters warnings }
            summary { pnlDelta tradeCountDelta winRateDelta feesDelta finalEquityDelta }
            divergences {
              category
              tradeNumber
              msUtc
              message
              leftFillPrice
              rightFillPrice
            }
            firstDivergenceMsUtc
          }
        }
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{BACKEND_URL}/graphql",
            json={
                "query": query,
                "variables": {"leftId": left_id, "rightId": right_id},
            },
        )
        response.raise_for_status()
        body = response.json()

    if "errors" in body:
        raise AssertionError(f"GraphQL errors: {body['errors']}")
    return body["data"]["compareBacktestRuns"]


def _write_reconciliation_report(
    comparison: dict,
    lean_id: int,
    engine_id: int,
    run_id: str,
) -> Path:
    REPORT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    out = REPORT_OUTPUT_DIR / f"ema_crossover_lean_vs_spec_{stamp}.json"
    out.write_text(
        json.dumps(
            {
                "generated_at_utc": stamp,
                "lean_run_id": run_id,
                "lean_strategy_execution_id": lean_id,
                "engine_strategy_execution_id": engine_id,
                "window_start": WINDOW_START.isoformat(),
                "window_end": WINDOW_END.isoformat(),
                "symbol": SYMBOL,
                "starting_cash": STARTING_CASH,
                "comparison": comparison,
            },
            indent=2,
            default=str,
        )
    )
    logger.info("Reconciliation report written to %s", out)
    return out


@pytest.mark.slow
@pytest.mark.asyncio
async def test_ema_crossover_lean_matches_spec_on_real_spy_data() -> None:
    """Acceptance gate: LEAN ema_crossover ≡ spec spy_ema_crossover.

    Fails if ANY divergence falls in the gating set. Always writes a JSON
    reconciliation report to ``/app/artifacts/parity-reports/`` (bind-mounted
    to ``PythonDataService/artifacts/parity-reports/`` on the host), pass or fail.
    """
    await _require_backend_reachable()
    await _require_compare_resolver_in_schema()
    await _require_launcher_reachable()
    _require_lean_data_for_window()
    _require_pinned_lean_image()

    # ---- 1. Run the LEAN trusted template ema_crossover via the launcher. ----
    run_id = f"parity_ema_{uuid4().hex[:12]}"
    # PR B: TrustedRunRequest carries a single ``data_policy`` block.
    from app.lean_sidecar.data_policy import BarsSpec, DataPolicy

    parity_data_policy = DataPolicy(
        source="synthetic",
        symbol=SYMBOL,
        adjusted=False,
        session="regular",
        input_bars=BarsSpec(timespan="minute", multiplier=1),
        strategy_bars=BarsSpec(timespan="minute", multiplier=15),
        timestamp_policy="bar_close_ms_utc",
        timezone="America/New_York",
        provider_kind="live",
        fixture_id=None,
        fixture_sha256=None,
    )
    lean_result = await run_trusted_sample(
        TrustedRunRequest(
            run_id=run_id,
            start_ms_utc=_date_to_ms_utc(WINDOW_START),
            end_ms_utc=_date_to_ms_utc(WINDOW_END),
            starting_cash=STARTING_CASH,
            template="ema_crossover",
            data_policy=parity_data_policy,
        )
    )
    if lean_result.strategy_execution_id is None:
        pytest.fail(
            f"LEAN run {run_id} did not persist (strategy_execution_id is None). "
            f"exit_code={lean_result.exit_code}, timed_out={lean_result.timed_out}, "
            f"lean_errors={lean_result.lean_errors}"
        )
    lean_id = lean_result.strategy_execution_id
    logger.info("LEAN run persisted as StrategyExecution.Id=%s", lean_id)

    # ---- 2. Run the spec engine on the same SPY data window. ----
    reader = LeanMinuteDataReader(LEAN_DATA_ROOTS)
    bars = list(reader.iter_bars(SYMBOL, WINDOW_START, WINDOW_END))
    if len(bars) < 50:
        pytest.skip(f"Insufficient SPY bars in window: only {len(bars)} bars available")

    spec_result = await run_spec_against_bars_and_persist(
        spec_path=SPEC_FIXTURE_PATH,
        symbol=SYMBOL,
        bars=bars,
        start_date=(WINDOW_START.year, WINDOW_START.month, WINDOW_START.day),
        end_date=(WINDOW_END.year, WINDOW_END.month, WINDOW_END.day),
        starting_cash=Decimal(str(STARTING_CASH)),
        backend_url=BACKEND_URL,
        strategy_name=STRATEGY_NAME,
        extra_statistics={
            "engine": "spec",
            "fill_mode": "signal_bar_close",
            "lean_run_id_paired_with": run_id,
        },
    )
    if spec_result.strategy_execution_id is None:
        pytest.fail("Spec engine run did not persist (strategy_execution_id is None)")
    engine_id = spec_result.strategy_execution_id
    logger.info("Spec run persisted as StrategyExecution.Id=%s", engine_id)

    # ---- 3. Query compareBacktestRuns over GraphQL. ----
    comparison = await _query_compare_backtest_runs(lean_id, engine_id)
    assert comparison is not None, "compareBacktestRuns returned null (one or both ids not found)"

    # ---- 4. Always write the reconciliation report (pass or fail). ----
    report_path = _write_reconciliation_report(comparison, lean_id, engine_id, run_id)

    # ---- 5. Assert zero gating divergences. ----
    divergences = comparison["divergences"]
    gating = [d for d in divergences if d["category"] in GATING_CATEGORIES]
    if gating:
        summary_lines = [f"  {d['category']} @ trade #{d.get('tradeNumber')}: {d['message']}" for d in gating]
        pytest.fail(
            f"{len(gating)} gating divergences (out of {len(divergences)} total)\n"
            + "\n".join(summary_lines)
            + f"\n\nFull report: {report_path}\n"
            + f"LEAN StrategyExecution.Id={lean_id}, "
            f"Spec StrategyExecution.Id={engine_id}"
        )

    logger.info(
        "PARITY PASS: 0 gating divergences across %d total. LEAN=%s, Spec=%s",
        len(divergences),
        lean_id,
        engine_id,
    )
