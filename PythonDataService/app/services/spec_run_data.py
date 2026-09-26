"""Lake admission and data identity for Spec and its research-run consumers.

The same materializer and root resolver as Strategy Lab own coverage and
adjustment. Spec uses Lab's default: Polygon split-adjusted minute bars,
filtered to the regular session. The reader retains its materialization
receipt so callers record the admitted lake state alongside their results.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path

from app.data_lake import run_materialization
from app.data_lake.run_materialization import EngineRunMaterialization
from app.engine.data.availability import MissingSessionsError, check_availability
from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.data.policy_store import resolve_data_roots


class MaterializedSpecReader(LeanMinuteDataReader):
    """The canonical minute reader with the lake admission receipt attached.

    Bar decoding, session filtering and iteration are inherited unchanged.
    The hash describes the materialized lake state, including supporting
    artifacts; it is not a byte-exact digest of only the consumed bars.
    """

    def __init__(self, roots: list[Path], materialization: EngineRunMaterialization) -> None:
        super().__init__(roots, session="regular")
        self.materialization = materialization


SpecDataSourceFactory = Callable[[str, date, date], LeanMinuteDataReader]


def materialize_spec_data_source(symbol: str, start: date, end: date) -> MaterializedSpecReader:
    """Admit a complete window through Lab's lake gate before building a reader.

    Called on the backtest worker thread, like Strategy Lab. Materialization
    owns provider fetching, catalog coordination and coverage refusal. The
    content check preserves #2445's stricter refusal for unreadable zips or
    a session whose file exists but contains no regular-hours bars.
    """
    materialization = run_materialization.materialize_engine_run(
        symbol=symbol,
        start=start,
        end=end,
        resolution="minute",
        price_adjustment_mode="polygon_split_adjusted",
        requester="strategy_spec",
    )
    roots = resolve_data_roots(source="polygon", adjusted=True)
    coverage = check_availability(roots, symbol, start, end, resolution="minute")
    if not coverage.is_complete:
        raise MissingSessionsError(coverage)
    return MaterializedSpecReader(roots, materialization)
