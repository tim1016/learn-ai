"""Cash dividends derived from LEAN factor files.

Formula: distribution = round_half_even(close − close · pf_i/pf_i+1, 2),
  paid as distribution × held quantity on the first bar after the
  cum-dividend session's close.
Reference: LEAN Common/Data/Market/Dividend.cs (ComputeDistribution) and
  Common/Data/Auxiliary/CorporateFactorProvider.cs (GetSplitsAndDividends —
  the first row per factor date; references/lean/
  7986ed0aade3ae5de06121682409f05984e32ff7), issue #2903.
Canonical implementation: this file owns per_share and the schedule; the
  payout credit (per_share × held quantity) is Portfolio.apply_dividend's,
  which cites this block's Reference as its own. BacktestEngine settles
  the schedule.
Validated against: tests/engine/test_engine_dividends.py and
  tests/integration/reconciliation/test_ema_previous_crossover_lean_golden.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import pairwise
from pathlib import Path, PurePosixPath

from app.data_lake.path_policy import LeanFactorFilePath
from app.lean_sidecar.workspace import TICKER_SYMBOL_PATTERN, SymbolValidationError, validate_symbol


@dataclass(frozen=True)
class CashDividend:
    """One distribution a run may pay on a position held through ``held_through_ms``.

    ``held_through_ms`` is the scheduled NYSE session close (int64 ms UTC)
    of the last cum-dividend session — the factor row's date. On the minute
    bars the LEAN-compatibility profile runs, the engine credits
    ``per_share × position`` on the first bar whose end is past it, which is
    the ex-date's first bar. (A daily bar spans the close, so the same
    comparison settles one bar earlier; the profile is minute-only, and no
    daily caller passes a schedule.)
    """

    symbol: str
    held_through_ms: int
    per_share: Decimal


def find_factor_file(data_root: Path | str | Sequence[Path | str], symbol: str) -> tuple[Path, PurePosixPath] | None:
    """Locate ``symbol``'s factor file across the roots: ``(absolute, root-relative)``.

    The location is :class:`LeanFactorFilePath`'s — the one place the repo
    names it — searched root by root with the same first-hit overlay order
    :class:`LeanMinuteDataReader` applies to bars, so the dividends a run
    pays come from the same tree its bars came from. The relative half is
    the key a compatibility fixture receipts the file under; ``None`` when
    no root holds one. A ``symbol`` that is not a plain ticker is refused
    here, where it first becomes a filesystem path.
    """
    # The boundary check raises on a symbol that is not a ticker; the value
    # used in the path is then reconstructed from the regex capture, because
    # CodeQL's path-injection rule treats ``re.Match.group(0)`` as sanitized
    # and a helper's return value (``validate_symbol``'s) it does not — the
    # same constraint workspace.py documents for run ids.
    validate_symbol(symbol)
    match = TICKER_SYMBOL_PATTERN.fullmatch(symbol)
    if match is None:  # Defensive: validate_symbol already raised on this case.
        raise SymbolValidationError(f"symbol rejected on second check: {symbol!r}")
    safe_symbol = match.group(0).lower()
    roots: list[Path | str] = [data_root] if isinstance(data_root, (str, Path)) else list(data_root)
    for root in roots:
        relative = LeanFactorFilePath(market="usa", symbol=safe_symbol).relative_path()
        factor_file = Path(root) / relative
        if factor_file.exists():
            return factor_file, relative
    return None


def factor_file_key(symbol: str) -> str:
    """The manifest key a factor file is receipted under: its root-relative location."""
    validate_symbol(symbol)
    return LeanFactorFilePath(market="usa", symbol=symbol.lower()).relative_path().as_posix()


def dividends_from_factor_tree(
    data_root: Path | str | Sequence[Path | str],
    symbol: str,
    start: date,
    end: date,
) -> list[CashDividend]:
    """Read ``symbol``'s dividends from the first factor file across the roots.

    First hit wins — the same overlay convention :class:`LeanMinuteDataReader`
    applies to bars. A tree with no factor file yields no dividends: the run
    then pays exactly what its input can prove, which is what a LEAN run
    reading the same tree would pay.
    """
    found = find_factor_file(data_root, symbol)
    return [] if found is None else dividends_from_factor_file(found[0], symbol, start, end)


def dividends_from_factor_file(factor_file: Path, symbol: str, start: date, end: date) -> list[CashDividend]:
    """Every dividend whose cum-dividend session falls in ``[start, end]``, read from the tree."""
    return dividends_from_factor_bytes(factor_file.read_bytes(), symbol, start, end)


def dividends_from_factor_bytes(payload: bytes, symbol: str, start: date, end: date) -> list[CashDividend]:
    """Every dividend whose cum-dividend session falls in ``[start, end]``, from ``payload``.

    The receipt-verified entry point: a caller that hashes the factor file
    against a manifest hands the same bytes here, so the schedule is
    provably computed from the bytes that passed the check, never a second,
    later read of a path that may have changed under it (#3000 review).

    A factor-file row is dated the last session before its ex-date; the
    distribution is LEAN's own ``ComputeDistribution`` on the row's reference
    price and the ratio of its price factor to the next row's. C#'s
    ``Math.Round`` defaults to banker's rounding, hence ``ROUND_HALF_EVEN``.
    The final row has no successor and yields nothing: its next factor is
    not in the file. Rows come from the canonical factor-file parser
    (:func:`app.data_lake.factor_files.parse_factor_file`) — its loud
    malformed-row refusal and ascending-date sort are this reader's too.
    """
    # Lazy imports: app.data_lake.factor_files and app.lean_sidecar's
    # calendar pull heavy module trees the engine layer should not pay at
    # import time (the same pattern as lean_format's session filter).
    from app.data_lake.factor_files import parse_factor_file
    from app.lean_sidecar.trading_calendar import session_close_ms_utc

    rows = parse_factor_file(payload.decode("utf-8"))
    # LEAN walks its factor rows grouped by date and uses each date's first
    # row only (CorporateFactorProvider.GetSplitsAndDividends reading
    # SortedFactorFileData): two distributions sharing an ex-date collapse
    # into one, reconstructed from the first row's factor to the next
    # date's row.
    first_per_date = [row for i, row in enumerate(rows) if i == 0 or row.row_date != rows[i - 1].row_date]
    dividends: list[CashDividend] = []
    for row, next_row in pairwise(first_per_date):
        ratio = row.price_factor / next_row.price_factor
        if ratio == 1 or not start <= row.row_date <= end:
            continue
        per_share = (row.reference_price - row.reference_price * ratio).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_EVEN
        )
        dividends.append(
            CashDividend(
                symbol=symbol.upper(),
                held_through_ms=session_close_ms_utc(row.row_date),
                per_share=per_share,
            )
        )
    return dividends
