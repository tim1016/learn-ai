"""Regenerate a registered Signal Program's golden ``EvaluationTrace`` corpus.

PRD ``docs/prds/sealed-signal-program-to-governed-alpaca-bot.md`` Slice 5
promotes additional strategies through the governed Signal Program seam
established for ``ema_crossover_signal``. Each promotion needs its own golden
trace corpus (``tests/fixtures/golden/<name>/v1/trace-corpus.json``), but
until this script existed there was no command that produced one: the
existing ``ema-signal-session`` corpus was hand-authored, in violation of
``.claude/rules/numerical-rigor.md``'s requirement that every golden fixture
carry the command used to regenerate it.

This command is generic across every registered Signal Program — it takes no
program-specific branches. For the given ``--program`` registry key it:

1. reads that program's ``SignalProgramContract`` off ``_STRATEGY_REGISTRY``;
2. reads every cell's ``manifest.json`` under
   ``tests/fixtures/golden/cross-engine-studies/cells/`` (ticker and window
   dates only — never the LEAN-specific order/state artifacts inside, which
   describe a different strategy's reconciliation, not this program's);
3. selects the cells whose ticker is a member of
   ``contract.validated_symbols`` (an explicit rule, not a silent filter —
   see ``_select_cells``);
4. for each selected cell, builds the program from
   ``contract.validated_settings`` plus that cell's own ticker as ``symbol``,
   replays the cell's raw minute bars through ``BacktestEngine``, and records
   the resulting ``EvaluationTrace`` count and root; and
5. assembles the corpus JSON, in the same schema and key order as the
   existing ``ema-signal-session`` fixture, and either writes it
   (``atomic_write_bytes``) or diffs it against the committed file
   (``--check``) without writing.

Usage::

    python -m scripts.generate_signal_program_trace_corpus \\
        --program ema_crossover_signal \\
        --output tests/fixtures/golden/ema-signal-session/v1/trace-corpus.json \\
        --check

    python -m scripts.generate_signal_program_trace_corpus \\
        --program sma_crossover \\
        --output tests/fixtures/golden/sma-crossover-signal/v1/trace-corpus.json

``--output`` is a required, explicit path rather than one derived from
``program_key``/``program_version``: the committed EMA fixture already lives
under ``ema-signal-session/v1``, not the mechanically-derived
``ema-crossover-signal/v1`` its ``program_version`` would suggest, so
guessing a directory name from the registry identity would not even
reproduce the fixture this script exists to regenerate.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine
from app.engine.strategy.registry import _STRATEGY_REGISTRY, SignalProgramContract, StrategyRegistration
from app.engine.strategy.signal_program import trace_corpus_root, trace_root
from app.services.spec_strategy_runner import InMemoryDataReader
from app.utils.atomic_file import atomic_write_bytes

_SERVICE_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CELLS_ROOT = _SERVICE_ROOT / "tests/fixtures/golden/cross-engine-studies/cells"


class CorpusGenerationError(RuntimeError):
    """The requested registry key cannot produce a golden trace corpus."""


@dataclass(frozen=True)
class _CellManifest:
    """The identity facts this script needs from one cell's ``manifest.json``.

    Deliberately narrow: a cell's ``manifest.json`` also carries LEAN-specific
    reconciliation facts (broker model, trusted-sample name, LEAN's own
    strategy constants) that belong to whichever strategy that cell was
    originally captured to reconcile — not to the Signal Program being
    replayed here. Only the raw-data identity (ticker, window) is shared,
    generic evidence any program can reuse.
    """

    cell_id: str
    ticker: str
    start_date: date
    end_date: date

    @property
    def duration_days(self) -> int:
        return (self.end_date - self.start_date).days


def _discover_cells(cells_root: Path) -> list[_CellManifest]:
    """Read every cell's ``manifest.json`` under ``cells_root``."""
    cells: list[_CellManifest] = []
    for cell_dir in sorted(path for path in cells_root.iterdir() if path.is_dir()):
        manifest_path = cell_dir / "manifest.json"
        if not manifest_path.is_file():
            raise CorpusGenerationError(f"cell '{cell_dir.name}' has no manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        try:
            ticker = manifest["ticker"]
            window = manifest["window"]
            start_date = date.fromisoformat(window["start_date"])
            end_date = date.fromisoformat(window["end_date"])
        except (KeyError, ValueError) as exc:
            raise CorpusGenerationError(f"cell '{cell_dir.name}' has a malformed manifest.json") from exc
        cells.append(_CellManifest(cell_id=cell_dir.name, ticker=ticker, start_date=start_date, end_date=end_date))
    return cells


def _select_cells(contract: SignalProgramContract, cells: list[_CellManifest]) -> list[_CellManifest]:
    """Select and order the cells this program's corpus replays.

    Selection rule (explicit, never a silent filter): a cell is included iff
    its manifest ticker is a member of ``contract.validated_symbols``. A
    program whose validated symbols are a strict subset of the tickers
    present under ``cross-engine-studies/cells`` therefore replays fewer than
    all of them by design, not by accident.

    Ordering: grouped by ticker (alphabetical), then by window duration
    ascending within a ticker (the shortest window first), with the cell id
    as a final tiebreak for determinism. This is the order the existing
    ``ema-signal-session`` corpus's ten entries are already in -- e.g.
    ``QQQ_W3mo`` before ``QQQ_W6mo`` before ``QQQ_W12mo`` -- derived here from
    each cell's own manifest window dates rather than parsed out of the
    directory name.
    """
    selected = [cell for cell in cells if cell.ticker in contract.validated_symbols]
    return sorted(selected, key=lambda cell: (cell.ticker, cell.duration_days, cell.cell_id))


def _minute_bars(cell_dir: Path, symbol: str) -> list[TradeBar]:
    """Read one cell's raw minute bars -- the only artifact this script reuses."""
    bars: list[TradeBar] = []
    with (cell_dir / "lean" / "observations.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            end_ms = int(row["ms_utc"])
            bars.append(
                TradeBar(
                    symbol=symbol,
                    start_ms=end_ms - 60_000,
                    end_ms=end_ms,
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    volume=int(Decimal(row["volume"])),
                )
            )
    return bars


def _entry_for_cell(
    registration: StrategyRegistration,
    contract: SignalProgramContract,
    cell: _CellManifest,
    *,
    cells_root: Path,
) -> dict[str, Any]:
    params = registration.param_schema(**{**contract.validated_settings, "symbol": cell.ticker})
    strategy = registration.build(params)
    minute_bars = _minute_bars(cells_root / cell.cell_id, cell.ticker)
    BacktestEngine(InMemoryDataReader(minute_bars)).run(strategy)
    program = getattr(strategy, "signal_program", None)
    if program is None:
        raise CorpusGenerationError(
            f"registration '{registration.display_name}' built a strategy with no active signal_program"
        )
    traces = program.session.traces
    return {
        "cell": cell.cell_id,
        "settings": strategy.signal_program_settings(),
        "trace_count": len(traces),
        "trace_root": trace_root(traces),
    }


def generate_corpus(program_key: str, *, cells_root: Path = _DEFAULT_CELLS_ROOT) -> dict[str, Any]:
    """Replay every selected cell and assemble the corpus payload (unformatted)."""
    registration = _STRATEGY_REGISTRY.get(program_key)
    if registration is None:
        raise CorpusGenerationError(f"'{program_key}' is not a registered strategy")
    contract = registration.signal_program_contract
    if contract is None or registration.signal_program_factory is None:
        raise CorpusGenerationError(f"'{program_key}' is not a registered, qualifiable Signal Program")

    cells = _select_cells(contract, _discover_cells(cells_root))
    if not cells:
        raise CorpusGenerationError(
            f"no cross-engine-studies cells matched validated_symbols={contract.validated_symbols!r}"
        )
    entries = [_entry_for_cell(registration, contract, cell, cells_root=cells_root) for cell in cells]
    return {
        "schema_version": 1,
        "program_key": program_key,
        "program_version": contract.program_version,
        "entries": entries,
        "trace_root": trace_corpus_root(entries),
    }


def format_corpus_text(corpus: dict[str, Any]) -> str:
    """Render the corpus payload byte-for-byte like the committed EMA fixture.

    The top level is hand-indented (2 spaces, one field per line); each
    ``entries`` element is its own compact ``sort_keys=True`` JSON object on
    one indented line, not further pretty-printed. Plain
    ``json.dumps(corpus, indent=2)`` would indent every nested field
    uniformly and would not reproduce the existing file, so this mirrors its
    exact layout instead of reformatting it.
    """
    entries = corpus["entries"]
    entry_lines = []
    for index, entry in enumerate(entries):
        suffix = "," if index < len(entries) - 1 else ""
        entry_lines.append(f"    {json.dumps(entry, sort_keys=True)}{suffix}")
    lines = [
        "{",
        '  "schema_version": 1,',
        f'  "program_key": {json.dumps(corpus["program_key"])},',
        f'  "program_version": {json.dumps(corpus["program_version"])},',
        '  "entries": [',
        *entry_lines,
        "  ],",
        f'  "trace_root": {json.dumps(corpus["trace_root"])}',
        "}",
    ]
    return "\n".join(lines) + "\n"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="generate_signal_program_trace_corpus",
        description=(
            "Replay a registered Signal Program's validated-settings corpus against the "
            "cross-engine-studies cells and emit (or check) its golden trace-corpus fixture."
        ),
    )
    parser.add_argument("--program", required=True, help="Registry key, e.g. 'ema_crossover_signal'.")
    parser.add_argument("--output", required=True, type=Path, help="Path to the trace-corpus.json fixture.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when the committed fixture differs from a freshly generated one. Never writes.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        corpus = generate_corpus(args.program)
    except CorpusGenerationError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    text = format_corpus_text(corpus)

    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.is_file() else None
        if current == text:
            sys.stdout.write(f"{args.output} is up to date.\n")
            return 0
        sys.stderr.write(
            f"{args.output} is stale; regenerate with:\n"
            f"  python -m scripts.generate_signal_program_trace_corpus "
            f"--program {args.program} --output {args.output}\n"
        )
        return 1

    atomic_write_bytes(args.output, text.encode("utf-8"))
    sys.stdout.write(f"Wrote {len(corpus['entries'])} entr{'y' if len(corpus['entries']) == 1 else 'ies'} to {args.output}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
