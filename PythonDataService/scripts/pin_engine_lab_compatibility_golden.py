"""Pin Engine Lab Python run 95 / LEAN run 96 as an immutable fixture.

The exporter accepts only the retained source artifact and database rows whose
identities are frozen below.  It is deliberately not a general-purpose
"approve current output" command: a different run requires a new versioned
fixture and reviewed source receipts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import urllib.request
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

FIXTURE_ID = "engine-lab-compatibility-95-96-v1"
PYTHON_RUN_ID = 95
LEAN_RUN_ID = 96
PARITY_GROUP_ID = "pg-d2236694f523410fbb91"
LEAN_RUN_NAME = f"companion-{PARITY_GROUP_ID}"
BAR_FIXTURE_ID = "bar-store-v1-6e96f0f2c5383e2d"
BAR_FIXTURE_SHA256 = "6e96f0f2c5383e2d1ad40f7308433aacc862bbfd6a9828f0b263cade70f0460e"
LEAN_SOURCE_COMMIT = "261366a7e26ae942df858ab20df4fef8fa07de67"
LEAN_IMAGE_DIGEST = "sha256:3dd003372f1ef1981b4e80038e3f1c557f1fe414d1be531f485ef870f81a5771"
SOURCE_MANIFEST_SHA256 = "08b8e51c5758ee45b162626eaae090db416add8eb515d4497281511a9ecfbb51"
NORMALIZED_RESULT_SHA256 = "38e5c574080f2352a69e47e9ecd2bd5d003ff34ac2e2da10a12570a936bba33e"

DESTINATION = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "golden"
    / FIXTURE_ID
)

GRAPHQL_QUERY = """
query PinCompatibilityGolden {
  python: backtestRun(id: 95) {
    id engine source strategyName symbol leanRunId fillMode executedAt durationMs
    totalTrades winningTrades losingTrades winRate totalPnL initialCash finalEquity
    totalFees maxDrawdown sharpeRatio sortinoRatio profitFactor leanStatisticsJson
    leanAnalysisJson verdictJson verdictVersion verdictGrade verdictSignal parityGroupId
    parameters
    dataPolicy {
      source symbol adjusted session inputBars { timespan multiplier }
      strategyBars { timespan multiplier } timestampPolicy timezone providerKind
      fixtureId fixtureSha256
    }
    validationAnalytics {
      schemaVersion computedAtMs engine error
      horizons { key label startMsUtc endMsUtc hasFullCoverage netReturn tradeCount winRate profitFactor }
      timingCells { weekday weekdayLabel hourEt tradeCount winRate averageReturn }
      seasonality { month monthLabel observationCount medianCompoundedReturn }
      rollingTradeStability { tradeNumber endMsUtc windowSize averageReturn winRate }
    }
    trades {
      entryTimestamp exitTimestamp entryPrice exitPrice quantity pnL pnlPts pnlPct
      signalReason isSyntheticExit
    }
    parityVerdicts { status verdictJson }
  }
  lean: backtestRun(id: 96) {
    id engine source strategyName symbol leanRunId fillMode executedAt durationMs
    totalTrades winningTrades losingTrades winRate totalPnL initialCash finalEquity
    totalFees maxDrawdown sharpeRatio sortinoRatio profitFactor leanStatisticsJson
    leanAnalysisJson verdictJson verdictVersion verdictGrade verdictSignal parityGroupId
    parameters
    dataPolicy {
      source symbol adjusted session inputBars { timespan multiplier }
      strategyBars { timespan multiplier } timestampPolicy timezone providerKind
      fixtureId fixtureSha256
    }
    validationAnalytics {
      schemaVersion computedAtMs engine error
      horizons { key label startMsUtc endMsUtc hasFullCoverage netReturn tradeCount winRate profitFactor }
      timingCells { weekday weekdayLabel hourEt tradeCount winRate averageReturn }
      seasonality { month monthLabel observationCount medianCompoundedReturn }
      rollingTradeStability { tradeNumber endMsUtc windowSize averageReturn winRate }
    }
    trades {
      entryTimestamp exitTimestamp entryPrice exitPrice quantity pnL pnlPts pnlPct
      signalReason isSyntheticExit
    }
    parityVerdicts { status verdictJson }
  }
}
"""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(raw: str) -> Any:
    return json.loads(raw, parse_float=Decimal)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_json_safe(value), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _query_runs(graphql_url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        graphql_url,
        data=json.dumps({"query": GRAPHQL_QUERY}).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        envelope = _load_json(response.read().decode("utf-8"))
    if envelope.get("errors"):
        raise ValueError(f"GraphQL export failed: {envelope['errors']}")
    return envelope["data"]


def _validate_source(source_root: Path, runs: dict[str, Any]) -> dict[str, Any]:
    source_manifest_path = source_root / "manifest.json"
    normalized_path = source_root / "normalized" / "result.json"
    if _sha256_file(source_manifest_path) != SOURCE_MANIFEST_SHA256:
        raise ValueError("retained LEAN source manifest does not match run 96")
    if _sha256_file(normalized_path) != NORMALIZED_RESULT_SHA256:
        raise ValueError("retained normalized result does not match run 96")

    source_manifest = _load_json(source_manifest_path.read_text(encoding="utf-8"))
    if source_manifest["run_id"] != LEAN_RUN_NAME:
        raise ValueError("unexpected LEAN run identity")
    if source_manifest["comparison_group_id"] != PARITY_GROUP_ID:
        raise ValueError("unexpected comparison group")
    if source_manifest["data_policy"]["fixture_id"] != BAR_FIXTURE_ID:
        raise ValueError("unexpected shared bar fixture id")
    if source_manifest["data_policy"]["fixture_sha256"] != BAR_FIXTURE_SHA256:
        raise ValueError("unexpected shared bar fixture hash")
    runtime = source_manifest["lean_runtime_provenance"]
    if runtime["source_commit"] != LEAN_SOURCE_COMMIT:
        raise ValueError("unexpected LEAN source commit")
    if runtime["image_digest"] != LEAN_IMAGE_DIGEST:
        raise ValueError("unexpected LEAN image digest")
    if source_manifest["exit_code"] != 0:
        raise ValueError("the pinned LEAN source run did not exit cleanly")

    expected_rows = {"python": (PYTHON_RUN_ID, "PYTHON"), "lean": (LEAN_RUN_ID, "LEAN")}
    for key, (run_id, engine) in expected_rows.items():
        row = runs[key]
        if row["id"] != run_id or row["engine"] != engine:
            raise ValueError(f"unexpected {key} row identity")
        if row["parityGroupId"] != PARITY_GROUP_ID:
            raise ValueError(f"unexpected {key} parity group")
        policy = row["dataPolicy"]
        if policy["fixtureId"] != BAR_FIXTURE_ID or policy["fixtureSha256"] != BAR_FIXTURE_SHA256:
            raise ValueError(f"unexpected {key} input receipt")
        verdict = _load_json(row["verdictJson"])
        if verdict["status"] != "complete" or verdict["available_required_metrics"] != 17:
            raise ValueError(f"{key} readiness is not complete 17/17")

    parity_rows = runs["lean"]["parityVerdicts"]
    if len(parity_rows) != 1 or parity_rows[0]["status"] != "agree":
        raise ValueError("run 95/96 no longer has one AGREE parity verdict")
    parity = _load_json(parity_rows[0]["verdictJson"])
    if parity["status"] != "agree" or parity["divergences"]:
        raise ValueError("run 95/96 parity receipt is not divergence-free")
    if parity["native_metric_parity"]["native_metric_count"] != 66:
        raise ValueError("run 95/96 does not cover all 66 native metrics")
    if parity["native_metric_parity"]["formatted_metric_count"] != 25:
        raise ValueError("run 95/96 does not cover all 25 formatted metrics")
    return source_manifest


def _copy_source_artifacts(source_root: Path) -> None:
    fixed_files = {
        "manifest.json": "source/lean-run-manifest.json",
        "normalized/result.json": "normalized/result.json",
        # Preserve the trusted LEAN algorithm byte-for-byte without exposing
        # the Oracle source as executable pytest/ruff discovery input.
        "workspace/project/main.py": "workspace/project/main.py.txt",
        "workspace/project/config.json": "workspace/project/config.json",
        "workspace/output/MyAlgorithm.json": "workspace/output/MyAlgorithm.json",
        "workspace/output/MyAlgorithm-summary.json": "workspace/output/MyAlgorithm-summary.json",
        "workspace/output/MyAlgorithm-order-events.json": "workspace/output/MyAlgorithm-order-events.json",
        "workspace/output/storage/observations.csv": "workspace/output/storage/observations.csv",
        "workspace/output/storage/state.csv": "workspace/output/storage/state.csv",
        "workspace/data/alternative/interest-rate/usa/interest-rate.csv": (
            "workspace/data/alternative/interest-rate/usa/interest-rate.csv"
        ),
        "workspace/data/market-hours/market-hours-database.json": (
            "workspace/data/market-hours/market-hours-database.json"
        ),
        "workspace/data/symbol-properties/security-database.csv": (
            "workspace/data/symbol-properties/security-database.csv"
        ),
        "workspace/data/symbol-properties/symbol-properties-database.csv": (
            "workspace/data/symbol-properties/symbol-properties-database.csv"
        ),
    }
    for source_relative, destination_relative in fixed_files.items():
        source = source_root / source_relative
        destination = DESTINATION / destination_relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    equity_root = source_root / "workspace" / "data" / "equity"
    for source in sorted(equity_root.rglob("*.zip")):
        relative = source.relative_to(source_root)
        destination = DESTINATION / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def _write_bar_manifest(source_manifest: dict[str, Any]) -> None:
    start = date.fromisoformat(source_manifest["parameters"]["start_date"])
    end = date.fromisoformat(source_manifest["parameters"]["end_date"])
    files: list[dict[str, Any]] = []
    current = start
    while current <= end:
        relative = Path("equity") / "usa" / "minute" / "spy" / f"{current:%Y%m%d}_trade.zip"
        path = DESTINATION / "workspace" / "data" / relative
        if path.is_file():
            files.append(
                {
                    "trading_date": current.isoformat(),
                    "path": relative.as_posix(),
                    "sha256": _sha256_file(path),
                    "size_bytes": path.stat().st_size,
                }
            )
        current += timedelta(days=1)
    receipt = {
        "schema_version": 1,
        "symbol": "SPY",
        "adjusted": False,
        "session": "regular",
        "files": files,
    }
    canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fixture_sha256 = hashlib.sha256(canonical).hexdigest()
    if fixture_sha256 != BAR_FIXTURE_SHA256:
        raise ValueError(f"copied trade ZIP snapshot changed: {fixture_sha256}")
    _write_json(
        DESTINATION / "input" / "bars-manifest.json",
        receipt
        | {
            "fixture_id": BAR_FIXTURE_ID,
            "fixture_sha256": fixture_sha256,
        },
    )


def _canonical_run(row: dict[str, Any]) -> dict[str, Any]:
    verdict = _load_json(row["verdictJson"])
    native_statistics = _load_json(row["leanStatisticsJson"])
    analysis = _load_json(row["leanAnalysisJson"]) if row.get("leanAnalysisJson") else []
    data_policy = row["dataPolicy"]
    return {
        "source_run_id": row["id"],
        "engine": row["engine"].lower(),
        "source": row["source"],
        "lean_run_id": row["leanRunId"],
        "strategy_name": row["strategyName"],
        "symbol": row["symbol"],
        "fill_mode": row["fillMode"],
        "executed_at_ms": row["executedAt"],
        "duration_ms": row["durationMs"],
        "parameters": _load_json(row["parameters"]),
        "data_policy": {
            "source": data_policy["source"],
            "symbol": data_policy["symbol"],
            "adjusted": data_policy["adjusted"],
            "session": data_policy["session"],
            "input_bars": data_policy["inputBars"],
            "strategy_bars": data_policy["strategyBars"],
            "timestamp_policy": data_policy["timestampPolicy"],
            "timezone": data_policy["timezone"],
            "provider_kind": data_policy["providerKind"],
            "fixture_id": data_policy["fixtureId"],
            "fixture_sha256": data_policy["fixtureSha256"],
        },
        "headline_kpis": {
            "total_trades": row["totalTrades"],
            "winning_trades": row["winningTrades"],
            "losing_trades": row["losingTrades"],
            "win_rate": row["winRate"],
            "total_pnl": row["totalPnL"],
            "initial_cash": row["initialCash"],
            "final_equity": row["finalEquity"],
            "total_fees": row["totalFees"],
            "max_drawdown": row["maxDrawdown"],
            "sharpe_ratio": row["sharpeRatio"],
            "sortino_ratio": row["sortinoRatio"],
            "profit_factor": row["profitFactor"],
        },
        "trades": [
            {
                "entry_ms_utc": trade["entryTimestamp"],
                "exit_ms_utc": trade["exitTimestamp"],
                "entry_price": trade["entryPrice"],
                "exit_price": trade["exitPrice"],
                "quantity": trade["quantity"],
                "pnl": trade["pnL"],
                "pnl_points": trade["pnlPts"],
                "pnl_pct": trade["pnlPct"],
                "signal_reason": trade["signalReason"],
                "is_synthetic_exit": trade["isSyntheticExit"],
            }
            for trade in row["trades"]
        ],
        "native_statistics": native_statistics,
        "lean_analysis": analysis,
        "validation_analytics": row["validationAnalytics"],
        "readiness": {
            "verdict_version": verdict["verdict_version"],
            "status": verdict["status"],
            "composite": verdict["composite"],
            "grade": verdict["grade"],
            "signal": verdict["signal"],
            "dimensions": verdict["dimensions"],
            "available_required_metrics": verdict["available_required_metrics"],
            "required_metrics": verdict["required_metrics"],
            "missing_required_metrics": verdict["missing_required_metrics"],
            "parity_signature": verdict["parity_signature"],
        },
    }


def _write_expected(runs: dict[str, Any], source_manifest: dict[str, Any]) -> dict[str, Any]:
    python = _canonical_run(runs["python"])
    lean = _canonical_run(runs["lean"])
    parity = _load_json(runs["lean"]["parityVerdicts"][0]["verdictJson"])
    expected = {
        "fixture_schema_version": 1,
        "fixture_id": FIXTURE_ID,
        "source_runs": {"python": PYTHON_RUN_ID, "lean": LEAN_RUN_ID},
        "parity_group_id": PARITY_GROUP_ID,
        "window": source_manifest["effective_algorithm_window_ms"],
        "python": python,
        "lean": lean,
        "parity_verdict": parity,
    }
    _write_json(DESTINATION / "expected" / "pair.json", expected)
    return expected


def _write_attribution(expected: dict[str, Any]) -> None:
    parity = expected["parity_verdict"]
    content = f"""# Engine Lab compatibility runs 95 / 96 golden fixture

## Independent Oracle

- Python Engine Lab source row: run {PYTHON_RUN_ID}.
- QuantConnect LEAN source row: run {LEAN_RUN_ID}, retained artifact `{LEAN_RUN_NAME}`.
- LEAN source commit: `{LEAN_SOURCE_COMMIT}`.
- LEAN image: `{LEAN_IMAGE_DIGEST}`.
- Compatibility contract: `us-equity-raw-ibkr-v1`.

## Inputs

- SPY, raw one-minute trade bars, regular session.
- Requested run dates: 2026-07-08 through 2026-08-07.
- Strategy cadence: 15 minutes; starting cash: USD 100,000.
- Shared fixture: `{BAR_FIXTURE_ID}` / `{BAR_FIXTURE_SHA256}`.
- Every staged ZIP and supporting LEAN data file is committed under `workspace/data/`
  and SHA-256 covered by `manifest.json`; tests never call Polygon.

## Pinned result

- Five closed trades with no timestamp, direction, quantity, fill-price, fee,
  P&L, or order-type divergence.
- Production Readiness v2: 17/17 inputs, C / 44 / Rework on both engines.
- LEAN native statistics: {parity['native_metric_parity']['native_metric_count']} numeric values and
  {parity['native_metric_parity']['formatted_metric_count']} formatted dashboard values matched.
- All three LEAN analysis findings are retained in `expected/pair.json` and
  the raw LEAN result.
- Final paired verdict: `AGREE`.

## Regeneration

Generated once from the retained run-96 artifact and live run-95/96 database
rows with:

```text
python scripts/pin_engine_lab_compatibility_golden.py \\
  /path/to/companion-pg-d2236694f523410fbb91 \\
  --graphql-url http://localhost:5050/graphql
```

The exporter refuses any different artifact hash, run IDs, group ID, bar
fixture, LEAN source/image, incomplete readiness receipt, or non-AGREE verdict.
A future re-baseline must use a new versioned fixture directory.

## Validation

```text
python -m pytest tests/integration/reconciliation/test_engine_lab_compatibility_golden.py
```

Tolerance contracts are fixed at $0.01 for fill prices, `rtol=0`,
`atol=1e-6` for accumulated P&L, `atol=0` for quantities/timestamps, and
`atol=0.0000500001` for LEAN native displayed-precision statistics.
"""
    (DESTINATION / "attribution.md").write_text(content, encoding="utf-8")


def _write_manifest(source_manifest: dict[str, Any], expected: dict[str, Any]) -> None:
    artifact_receipts: dict[str, dict[str, int | str]] = {}
    for path in sorted(DESTINATION.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        relative = path.relative_to(DESTINATION).as_posix()
        artifact_receipts[relative] = {
            "sha256": _sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
    manifest = {
        "fixture_schema_version": 1,
        "fixture_id": FIXTURE_ID,
        "oracle": "QuantConnect LEAN run 96 paired with Python Engine Lab run 95",
        "source_runs": expected["source_runs"],
        "captured_at_ms_utc": expected["parity_verdict"]["computed_at_ms"],
        "parity_group_id": PARITY_GROUP_ID,
        "comparison_contract_id": source_manifest["comparison_contract_id"],
        "bar_fixture_id": BAR_FIXTURE_ID,
        "bar_fixture_sha256": BAR_FIXTURE_SHA256,
        "lean_source_commit": LEAN_SOURCE_COMMIT,
        "lean_image_digest": LEAN_IMAGE_DIGEST,
        "source_manifest_sha256": SOURCE_MANIFEST_SHA256,
        "normalized_result_sha256": NORMALIZED_RESULT_SHA256,
        "generator": "scripts/pin_engine_lab_compatibility_golden.py",
        "generator_sha256": _sha256_file(Path(__file__).resolve()),
        "timestamp_convention": "int64_ms_utc",
        "tolerances": {
            "timestamps_ms": "0",
            "quantities": "0",
            "fill_price_atol": "0.01",
            "pnl_atol": "0.000001",
            "native_statistics_atol": "0.0000500001",
            "rtol": "0",
        },
        "artifacts": artifact_receipts,
    }
    _write_json(DESTINATION / "manifest.json", manifest)


def pin(source_root: Path, graphql_url: str) -> None:
    if DESTINATION.exists():
        raise FileExistsError(
            f"{DESTINATION} already exists; golden fixtures are immutable. "
            "Create a new versioned fixture for a re-baseline."
        )
    runs = _query_runs(graphql_url)
    source_manifest = _validate_source(source_root, runs)
    DESTINATION.mkdir(parents=True)
    _copy_source_artifacts(source_root)
    _write_bar_manifest(source_manifest)
    expected = _write_expected(runs, source_manifest)
    _write_attribution(expected)
    _write_manifest(source_manifest, expected)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_root", type=Path)
    parser.add_argument("--graphql-url", default="http://localhost:5050/graphql")
    args = parser.parse_args()
    pin(args.source_root.resolve(), args.graphql_url)


if __name__ == "__main__":
    main()
