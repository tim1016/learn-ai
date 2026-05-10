"""Orchestrator: rule + bars + window -> artifact directory.

Has two public entry points:
  * ``generate_prediction_set(...)`` -- programmatic API used by tests.
  * ``main(argv)`` -- CLI bound to ``__main__``.

The CLI defers actual market-data reading to a ``bars_provider`` callable.
The default provider (used by the CLI) wraps the LEAN minute reader; tests
inject synthetic providers.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Callable, Iterable
from datetime import date as Date
from pathlib import Path

from app.research.ml.artifact import (
    ChunkRef,
    PredictionSetManifest,
    compute_prediction_set_hash,
    compute_rows_hash,
    write_chunk_rows,
)
from app.research.ml.coverage import iter_consolidated_bars
from app.research.ml.generators.deterministic_rule import (
    RULE_ID as RSI_RULE_ID,
)
from app.research.ml.generators.deterministic_rule import (
    RULE_VERSION as RSI_RULE_VERSION,
)
from app.research.ml.generators.deterministic_rule import (
    compute_rsi_14_centered_predictions,
)

logger = logging.getLogger(__name__)


BarsProvider = Callable[..., Iterable[tuple[float, int]]]
"""(close, timestamp_ms) pairs for the bars the run will see."""


_RULES: dict[str, tuple[str, str, Callable]] = {
    "rsi_14_centered": (RSI_RULE_ID, RSI_RULE_VERSION, compute_rsi_14_centered_predictions),
}


def _set_id_for(rule: str, symbol: str, start: Date, end: Date, resolution_minutes: int) -> str:
    return (
        f"pred_{symbol.lower()}_{rule}_"
        f"{start.isoformat()}_{end.isoformat()}_"
        f"{resolution_minutes}m_v1"
    )


def generate_prediction_set(
    *,
    rule: str,
    symbol: str,
    start: Date,
    end: Date,
    resolution_minutes: int,
    artifacts_root: Path,
    bars_provider: BarsProvider,
) -> str:
    """Write a complete artifact directory under ``artifacts_root``.

    Returns the ``prediction_set_id`` used as the directory name.
    Overwrites any existing directory with the same id.
    """
    if rule not in _RULES:
        raise ValueError(f"unknown rule {rule!r}; known: {sorted(_RULES)}")
    rule_id, rule_version, rule_fn = _RULES[rule]

    pairs = list(bars_provider(symbol=symbol, start=start, end=end, resolution_minutes=resolution_minutes))
    if not pairs:
        raise ValueError(f"bars_provider returned no bars for {symbol} {start}..{end}")
    closes = [c for c, _ in pairs]
    timestamps_ms = [ts for _, ts in pairs]

    rows = rule_fn(closes, timestamps_ms, symbol=symbol)

    set_id = _set_id_for(rule, symbol, start, end, resolution_minutes)
    set_dir = artifacts_root / set_id
    chunks_dir = set_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    trained_through_ms = timestamps_ms[0] - 1
    chunk_path = chunks_dir / f"{trained_through_ms}.parquet"
    write_chunk_rows(chunk_path, rows, field_names=["prediction"])

    chunk_meta = ChunkRef(
        trained_through_ms=trained_through_ms,
        start_ms=timestamps_ms[0],
        end_ms=timestamps_ms[-1],
        row_count=len(rows),
        rows_hash=compute_rows_hash(rows),
    )

    manifest_dict = {
        "schema_version": "1.0",
        "prediction_set_id": set_id,
        "symbol": symbol,
        "resolution_minutes": resolution_minutes,
        "field_names": ["prediction"],
        "warmup_policy": "neutral_zero_until_feature_ready",
        "generator": {"kind": "deterministic_rule", "rule_id": rule_id, "rule_version": rule_version},
        "chunks": [chunk_meta.model_dump()],
        "prediction_set_hash": "0" * 64,
    }
    manifest_dict["prediction_set_hash"] = compute_prediction_set_hash(manifest_dict)

    PredictionSetManifest.model_validate(manifest_dict)
    (set_dir / "manifest.json").write_text(json.dumps(manifest_dict, sort_keys=True))

    logger.info(
        "[ML] wrote prediction set %s (%d rows, hash=%s)",
        set_id,
        len(rows),
        manifest_dict["prediction_set_hash"][:12],
    )
    return set_id


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_lean_bars_provider(
    *, symbol: str, start: Date, end: Date, resolution_minutes: int
) -> Iterable[tuple[float, int]]:
    """Yield ``(close, timestamp_ms)`` for each bar the engine will see.

    Constructs a ``LeanMinuteDataReader`` from ``LEAN_DATA_ROOT`` /
    ``LEAN_DATA_CACHE`` (mirroring ``app/routers/spec_strategy.py``'s
    default factory) and drives the same ``TradeBarConsolidator``
    configuration the engine uses internally via
    :func:`app.research.ml.coverage.iter_consolidated_bars`. This makes
    the artifact's bar clock identical to the engine's at run time.
    """
    # Imported lazily so the module stays importable without LEAN configured
    # (tests inject a synthetic provider via ``bars_provider=...``).
    from app.engine.data.lean_format import LeanMinuteDataReader

    roots: list[Path] = []
    for env_var in ("LEAN_DATA_ROOT", "LEAN_DATA_CACHE"):
        val = os.environ.get(env_var)
        if val:
            roots.append(Path(val))
    if not roots:
        raise RuntimeError(
            "No LEAN data roots configured (set LEAN_DATA_ROOT or LEAN_DATA_CACHE)"
        )
    reader = LeanMinuteDataReader(roots)

    for bar in iter_consolidated_bars(
        reader,
        symbol=symbol,
        start_date=start,
        end_date=end,
        resolution_minutes=resolution_minutes,
    ):
        yield float(bar.close), int(bar.end_time.timestamp() * 1000)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="generate_prediction_set")
    parser.add_argument("--rule", required=True, choices=sorted(_RULES))
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", required=True, type=Date.fromisoformat)
    parser.add_argument("--end", required=True, type=Date.fromisoformat)
    parser.add_argument("--resolution-minutes", required=True, type=int)
    parser.add_argument(
        "--artifacts-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent.parent.parent / "artifacts" / "predictions",
    )
    args = parser.parse_args(argv)

    set_id = generate_prediction_set(
        rule=args.rule,
        symbol=args.symbol,
        start=args.start,
        end=args.end,
        resolution_minutes=args.resolution_minutes,
        artifacts_root=args.artifacts_root,
        bars_provider=_default_lean_bars_provider,
    )
    print(set_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
