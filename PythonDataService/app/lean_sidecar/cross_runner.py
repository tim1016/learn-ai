"""Phase 5g.2 — Engine-Lab cross-run primitive.

This module hosts the seam Phase 5g.3 will call from the
``POST /api/lean-sidecar/runs/{id}/cross-reconcile`` endpoint (currently
501-stub from Phase 5g.1). The primitive:

1. Resolves a caller-supplied Engine-Lab strategy class **by name**
   (D3 — no auto-derivation).
2. Wires a ``LeanMinuteDataReader`` at the LEAN-Lab run's
   ``<workspace>/data`` root — same staged zips LEAN itself consumed
   (D3 — shared staged data, not Engine-Lab's native fixtures).
3. Subclass-wraps the resolved strategy to pin the LEAN-Lab run's
   trading window + starting cash via ``initialize`` override (the
   strategy's own ``initialize`` runs first; cross-run overrides
   clobber start/end/cash AFTER ``super().initialize()`` so any defaults
   the strategy class hard-codes don't leak through).
4. Runs the backtest and normalizes ``OrderEvent``s to the same wire
   shape Phase 3a emits for LEAN's ``result.json`` order events — so
   the Phase 5g.3 reconciler can diff both sides symmetrically.

What this module does NOT do:

* The diff itself — Phase 5g.3 maps the normalized cross-run events
  against the LEAN-Lab run's normalized events and bins disagreements
  into ``DivergenceCategory``.
* Strategy parameter overrides beyond symbol / dates / cash —
  Phase 5g.2's contract is the minimal seam; richer parameter
  customization (window sizes, thresholds) lands when an actual
  caller needs it.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.engine import BacktestEngine
from app.engine.execution.order import OrderEvent
from app.engine.execution.sizing import LeanSetHoldingsSizing
from app.engine.strategy.base import Strategy

_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")

# Single search package for strategy resolution. Keeping it explicit (not
# autodiscovering every Strategy subclass in the codebase) means a test-only
# strategy in a tests/ subtree can never accidentally become routable.
_ALGORITHMS_PACKAGE = "app.engine.strategy.algorithms"


class CrossRunError(Exception):
    """Base class for cross-run primitive errors. Phase 5g.3 maps these to
    HTTP responses on the cross-reconcile endpoint."""


class StrategyNotFoundError(CrossRunError):
    """The caller-supplied strategy class name does not resolve to a known
    Engine-Lab Strategy subclass under ``app.engine.strategy.algorithms``.

    Per D3 (mission-critical doc), there is no auto-derivation fallback —
    the caller MUST name a real class. The exception message includes the
    sorted list of resolvable names so the caller's UI / log can guide
    the operator without an extra round-trip."""


class WorkspaceDataMissingError(CrossRunError):
    """The LEAN-Lab run's workspace does not have a ``data/`` directory
    populated. Either the run hasn't completed staging yet, or the
    workspace was pruned between the LEAN-Lab run and this cross-run."""


class StrategyIncompatibleError(CrossRunError):
    """The resolved Strategy subclass does not accept the ``symbol`` kwarg
    in its constructor. The Phase 5g.2 cross-run contract requires
    strategies expose ``symbol`` so the cross-runner can pin the
    LEAN-Lab run's symbol through to the engine's data path. Strategies
    intentionally not cross-runnable (e.g., spec-driven strategies whose
    universe is data-derived) will fail this check by design."""


@dataclass(frozen=True)
class CrossRunOrderEvent:
    """One Engine-Lab fill normalized to the same wire shape as Phase 3a
    LEAN-Lab ``NormalizedOrderEvent``. Phase 5g.3 reconciler can compare
    both sides without per-side adapters.

    Fields chosen to mirror ``NormalizedOrderEvent``:
      * ``order_event_id``: synthetic 0-indexed counter — Engine-Lab's
        ``OrderEvent`` doesn't number fills separately, so this is the
        fill's position in the chronological order_events list.
      * ``order_id``: Engine-Lab's per-portfolio monotonic order id.
      * ``ms_utc``: bar end_time → UTC ms. Engine-Lab times are ET-aware;
        the converter normalizes through ``.astimezone(UTC)`` so a fill
        on an extended-hours bar lands on the same UTC ms LEAN would
        record.
      * ``direction``: "Buy" / "Sell" derived from the SIGN of
        ``fill_quantity`` (Engine-Lab's quantity is signed: positive=long,
        negative=short). LEAN's ``NormalizedOrderEvent.direction`` is
        already the Buy/Sell string.
      * ``fill_quantity``: ``abs(signed_qty)`` — sign info is carried by
        ``direction`` so the comparison is symmetric.
      * ``fill_price`` / ``fee``: Decimal preserved on the wire."""

    order_event_id: int
    order_id: int
    symbol: str
    ms_utc: int
    direction: Literal["Buy", "Sell"]
    fill_quantity: int
    fill_price: Decimal
    fee: Decimal
    tag: str = ""


@dataclass(frozen=True)
class CrossRunResult:
    """Cross-run output. Carries enough metadata for the reconciler to
    audit what was actually executed against the workspace data."""

    strategy_class_name: str
    symbol: str
    start_date: date
    end_date: date
    initial_cash: Decimal
    total_order_events: int
    order_events: list[CrossRunOrderEvent] = field(default_factory=list)


def resolve_strategy_class(name: str) -> type[Strategy]:
    """Look up a Strategy subclass by ``__name__`` under
    ``app.engine.strategy.algorithms``.

    Walked at call-time rather than module-import-time so a strategy
    added in a hot-reload dev cycle is picked up without restart. The
    iteration cost is negligible against the workspace I/O cost of the
    actual cross-run.

    Raises ``StrategyNotFoundError`` with the sorted list of known
    classes when the name doesn't match. Per D3, no auto-derivation or
    fuzzy-match fallback — the operator is expected to type the exact
    class name."""
    package = importlib.import_module(_ALGORITHMS_PACKAGE)
    candidates: dict[str, type[Strategy]] = {}
    for _, modname, _ in pkgutil.iter_modules(package.__path__):
        if modname.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{_ALGORITHMS_PACKAGE}.{modname}")
        except ImportError:
            # A strategy file with a broken import (e.g., optional dep
            # missing in this environment) should not crash resolution
            # for the OTHER strategies. Skip and continue.
            continue
        for attr_name, attr in vars(mod).items():
            if attr_name.startswith("_"):
                continue
            if not isinstance(attr, type):
                continue
            if not issubclass(attr, Strategy):
                continue
            if attr is Strategy:
                continue
            candidates[attr.__name__] = attr
    if name in candidates:
        return candidates[name]
    raise StrategyNotFoundError(f"unknown strategy class {name!r}; known: {sorted(candidates.keys())}")


def _instantiate_with_symbol(cls: type[Strategy], symbol: str) -> Strategy:
    """Instantiate a resolved strategy class, pinning ``symbol``.

    Raises ``StrategyIncompatibleError`` if the constructor signature
    doesn't accept the ``symbol`` keyword — the cross-run contract
    requires explicit symbol pinning so the engine reads the LEAN-Lab
    run's data zips, not some hardcoded default."""
    try:
        sig = inspect.signature(cls.__init__)
    except (TypeError, ValueError) as e:
        raise StrategyIncompatibleError(f"{cls.__name__}: cannot introspect constructor signature ({e})") from e
    if "symbol" not in sig.parameters:
        raise StrategyIncompatibleError(
            f"{cls.__name__}: constructor does not accept a 'symbol' kwarg; "
            "cross-runnable strategies must expose symbol as a kwarg per "
            "Phase 5g.2 contract"
        )
    return cls(symbol=symbol.upper())


def _normalize_order_events(
    order_events: list[OrderEvent],
    *,
    symbol_default: str,
) -> list[CrossRunOrderEvent]:
    """Map Engine-Lab ``OrderEvent``s to the cross-run wire shape.

    ``symbol_default`` is the cross-run primitive's input symbol; used
    only if an OrderEvent inexplicably has an empty symbol field
    (defense in depth — the engine should always populate it)."""
    normalized: list[CrossRunOrderEvent] = []
    for i, e in enumerate(order_events):
        t = e.time if e.time.tzinfo is not None else e.time.replace(tzinfo=_ET)
        ms_utc = int(t.astimezone(_UTC).timestamp() * 1000)
        direction_str: Literal["Buy", "Sell"] = "Buy" if e.fill_quantity >= 0 else "Sell"
        normalized.append(
            CrossRunOrderEvent(
                order_event_id=i,
                order_id=e.order_id,
                symbol=(e.symbol or symbol_default).upper(),
                ms_utc=ms_utc,
                direction=direction_str,
                fill_quantity=abs(int(e.fill_quantity)),
                fill_price=e.fill_price,
                fee=e.fee,
                tag=e.tag,
            )
        )
    return normalized


def run_engine_lab_on_workspace(
    workspace_path: Path,
    strategy_class_name: str,
    *,
    symbol: str,
    start_date: date,
    end_date: date,
    initial_cash: Decimal,
    output_dir: Path | None = None,
) -> CrossRunResult:
    """Run the resolved Engine-Lab strategy against ``<workspace_path>/data``.

    Parameters
    ----------
    workspace_path:
        The LEAN-Lab run's workspace root — same directory the launcher
        wrote ``manifest.json`` into. The Engine-Lab data reader reads
        ``<workspace_path>/data/equity/usa/minute/<symbol>/*_trade.zip``
        (the layout the LEAN-Lab orchestrator already staged).
        When ``<workspace_path>/data`` does not exist, ``workspace_path``
        itself is tried as the data root (supports callers that pass the
        Polygon capture root directly, where ``equity/…`` lives at the
        top level rather than under ``data/``).
    strategy_class_name:
        Caller-supplied class name. Must match a ``Strategy`` subclass
        ``__name__`` under ``app.engine.strategy.algorithms`` (per D3,
        no auto-derivation). Resolution uses ``resolve_strategy_class``.
    symbol:
        Ticker the LEAN-Lab run traded; pinned through to the strategy
        via the ``symbol`` constructor kwarg AND to the engine's
        ``LeanMinuteDataReader``.
    start_date / end_date:
        LEAN-Lab run's trading window (NY-local trading dates). The
        cross-runner subclass-wraps ``initialize`` to clobber whatever
        defaults the strategy hardcodes so the same NY-day boundaries
        LEAN saw are what Engine-Lab uses.
    initial_cash:
        LEAN-Lab run's starting capital, pinned the same way as the
        dates so position-sizing primitives (``SetHoldings``) target
        the same dollar amount on both engines.
    output_dir:
        When provided, passed to the strategy constructor as
        ``output_dir`` so the strategy emits ``observations.csv`` and
        ``state.csv`` into that directory. Task 10 — parity-matrix
        regeneration script needs these files for Gate 1 and Gate 2.

    Returns
    -------
    CrossRunResult with normalized order events ready for Phase 5g.3's
    diff against the LEAN-Lab run's parsed ``order_events``."""
    # Prefer workspace_path/data (canonical workspace layout); fall back
    # to workspace_path itself for capture roots where equity/ lives at
    # the top level (e.g. _lean_data_capture/<TICKER>/).
    candidate_data = workspace_path / "data"
    if candidate_data.exists() and candidate_data.is_dir():
        data_root = candidate_data
    elif workspace_path.exists() and workspace_path.is_dir():
        data_root = workspace_path
    else:
        raise WorkspaceDataMissingError(
            f"workspace data dir not found: {candidate_data} "
            "(was the LEAN-Lab run staged? did the workspace get pruned?)"
        )

    base_class = resolve_strategy_class(strategy_class_name)
    # Pass output_dir to the strategy constructor when provided so it
    # emits observations.csv + state.csv for the parity-matrix gates.
    if output_dir is not None and "output_dir" in inspect.signature(base_class.__init__).parameters:
        base_instance: Strategy = base_class(symbol=symbol.upper(), output_dir=output_dir)
    else:
        base_instance = _instantiate_with_symbol(base_class, symbol)

    pinned_start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=_ET)
    pinned_end = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59, tzinfo=_ET)
    pinned_cash = Decimal(initial_cash)

    # Capture the resolved class + the pinned values via closure into a
    # subclass that runs the base initialize() THEN clobbers dates/cash.
    # Done as a subclass (not by mutating attributes after the engine
    # calls initialize) because the engine calls initialize() itself
    # inside .run() — mutating the instance beforehand has no effect.
    class _CrossRunStrategy(base_class):  # type: ignore[misc,valid-type]
        def initialize(self) -> None:
            super().initialize()
            self.start_date = pinned_start
            self.end_date = pinned_end
            self.initial_cash = pinned_cash

    # Reuse the symbol-pinned instance's ``__dict__`` so any
    # constructor-time state (indicators, flags) is preserved when
    # promoting to the cross-run subclass. Cheaper than re-instantiating
    # the wrapper — and correct because both classes share the same
    # constructor signature (the wrapper doesn't override __init__).
    cross_instance = _CrossRunStrategy.__new__(_CrossRunStrategy)
    cross_instance.__dict__.update(base_instance.__dict__)

    reader = LeanMinuteDataReader(data_root=data_root)
    # Cross-engine parity runs size positions like LEAN: SetHoldings reserves
    # a free-portfolio-value buffer + the order fee. SimpleFloorSizing would
    # buy one share more than LEAN (Gate 3 QUANTITY_MISMATCH).
    engine = BacktestEngine(data_source=reader, sizing_model=LeanSetHoldingsSizing())
    result = engine.run(cross_instance)

    normalized = _normalize_order_events(result.order_events, symbol_default=symbol)
    return CrossRunResult(
        strategy_class_name=strategy_class_name,
        symbol=symbol.upper(),
        start_date=start_date,
        end_date=end_date,
        initial_cash=pinned_cash,
        total_order_events=len(normalized),
        order_events=normalized,
    )
