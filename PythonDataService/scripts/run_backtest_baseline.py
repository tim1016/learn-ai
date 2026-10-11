"""Run the frozen backtest baseline and write its report (#3034 slice 1, #3036).

The operator entry point the benchmark issue asks for: it runs
``app.research.benchmark.run_baseline`` over the local lake — offline, no
provider or broker — writes the machine-readable JSON report, and prints the
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
from datetime import date
from pathlib import Path

from app.research.benchmark import run_baseline, summarize_report


def _iso_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be an ISO date (YYYY-MM-DD), not {raw!r}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol", required=True, help="the symbol the corpus evaluates")
    parser.add_argument("--from", dest="development_from", type=_iso_date, required=True, help="development window's first ET trading date")
    parser.add_argument("--to", dest="development_to", type=_iso_date, required=True, help="development window's last ET trading date")
    parser.add_argument("--roots", type=Path, nargs="+", required=True, help="lake roots to capture (each holds equity/…)")
    parser.add_argument("--repetitions", type=int, default=3, help="times to run the corpus (default 3)")
    parser.add_argument("--output", type=Path, default=Path("baseline-report.json"), help="where to write the JSON report")
    args = parser.parse_args(argv)

    report = run_baseline(
        roots=args.roots,
        symbol=args.symbol,
        development=(args.development_from, args.development_to),
        repetitions=args.repetitions,
    )
    args.output.write_text(json.dumps(report.as_dict(), indent=2), encoding="utf-8")
    print(summarize_report(report))
    print(f"  report written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
