"""Run the frozen backtest baseline and write its report (#3034 slice 1, #3036).

The operator entry point the benchmark issue asks for: it runs
``app.research.benchmark.run_baseline`` over the local lake — offline, no
provider or broker — writes the machine-readable JSON report, and logs the
readable summary. Long/manual results live as issue evidence on #3036 or a
throwaway research branch; the report is never committed to master.

Usage::

    python -m scripts.run_backtest_baseline \\
        --symbol SPY --from 2024-01-02 --to 2024-12-31 \\
        --roots /path/to/lake/root --output baseline-report.json

The development window's dates are ET trading dates. ``--roots`` are the
lake roots the snapshot captures (the directory holding ``equity/…``), and
the engine's own data-root settings must resolve the same artifacts — the
snapshot's manifest binds every engine read to the receipted bytes, and a
root that does not line up fails the run rather than reading unreceipted
data.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

from app.research.benchmark import (
    DECODED_CACHE_DEFAULT_BYTES,
    RUNNABLE_EXECUTION_MODES,
    run_baseline,
    summarize_report,
)

logger = logging.getLogger(__name__)


def _iso_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be an ISO date (YYYY-MM-DD), not {raw!r}") from exc


def main(argv: list[str] | None = None) -> int:
    # The operator reads this summary from a terminal; stdout is the channel,
    # through the logger — never a bare print.
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol", required=True, help="the symbol the corpus evaluates")
    parser.add_argument("--from", dest="development_from", type=_iso_date, required=True, help="development window's first ET trading date")
    parser.add_argument("--to", dest="development_to", type=_iso_date, required=True, help="development window's last ET trading date")
    parser.add_argument("--roots", type=Path, nargs="+", required=True, help="lake roots to capture (each holds equity/…)")
    parser.add_argument("--repetitions", type=int, default=3, help="times to run the corpus (default 3)")
    parser.add_argument(
        "--execution-mode",
        choices=RUNNABLE_EXECUTION_MODES,
        default="serial-original",
        help="which executor to measure (default serial-original; serial-decoded-cache shares admitted decodes, #3038)",
    )
    parser.add_argument(
        "--cache-mb",
        type=int,
        default=DECODED_CACHE_DEFAULT_BYTES // (1024**2),
        help="decoded-artifact retention bound in MiB for serial-decoded-cache (default %(default)s)",
    )
    parser.add_argument("--output", type=Path, default=Path("baseline-report.json"), help="where to write the JSON report")
    args = parser.parse_args(argv)

    report = run_baseline(
        roots=args.roots,
        symbol=args.symbol,
        development=(args.development_from, args.development_to),
        repetitions=args.repetitions,
        execution_mode=args.execution_mode,
        decoded_cache_byte_bound=args.cache_mb * 1024**2,
    )
    args.output.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
    logger.info("%s", summarize_report(report))
    logger.info("  report written: %s", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
