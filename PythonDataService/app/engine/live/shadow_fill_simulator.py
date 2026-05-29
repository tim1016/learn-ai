"""ShadowFillSimulator (PRD-C).

Pure-function fill-model dispatch for shadow mode: synthesise a
``shadow_sim`` ``ExecutionRow`` from an intended order + the source bar the
strategy acted on + the next bar (whose open the fill prices against).
Deterministic over ``(intended_order, source_bar, next_bar, fill_model)`` —
no I/O, no randomness.

Supported models at PRD-C launch: ``NEXT_BAR_OPEN`` (fill at the next bar's
open, the shadow analogue of the executing path's next-bar-open fill). New
models are added as future strategies declare them, each a new dispatch arm
with its own golden fixture.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.broker.ibkr.models import IbkrOrderSpec
from app.engine.live.artifacts import ExecutionRow
from app.engine.live.divergence.bar_series_joiner import CanonicalBar

SUPPORTED_FILL_MODELS = frozenset({"NEXT_BAR_OPEN"})


@dataclass(frozen=True)
class PendingFill:
    """Returned when the fill cannot yet be priced — the next bar has not
    arrived. The adapter records the intent and re-simulates once it lands.
    No fake fill price is ever synthesised."""

    source_bar_close_ms: int
    fill_model: str


class UnknownFillModel(ValueError):
    """Raised for a fill model the simulator does not implement."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"unknown fill_model {name!r}; supported: {sorted(SUPPORTED_FILL_MODELS)}"
        )
        self.name = name


def _signed_quantity(spec: IbkrOrderSpec) -> int:
    qty = int(spec.quantity)
    return qty if spec.action == "BUY" else -qty


def _shadow_exec_id(strategy_instance_id: str, source_bar_close_ms: int, action: str) -> str:
    # ``shadow:``-prefixed so the id can never collide with a real IBKR execId
    # and any artifact cross-check landing at the broker will not find it.
    return f"shadow:{strategy_instance_id}:{source_bar_close_ms}:{action}"


def _shadow_client_order_id(
    strategy_instance_id: str, source_bar_close_ms: int, action: str
) -> str:
    return f"shadow-{strategy_instance_id}-{source_bar_close_ms}-{action}"


def simulate_shadow_fill(
    intended: IbkrOrderSpec,
    *,
    source_bar: CanonicalBar,
    next_bar: CanonicalBar | None,
    fill_model: str,
    account_id: str,
    strategy_instance_id: str,
) -> ExecutionRow | PendingFill:
    """Synthesise a shadow_sim ExecutionRow, or defer if the next bar is absent."""
    if fill_model not in SUPPORTED_FILL_MODELS:
        raise UnknownFillModel(fill_model)

    if next_bar is None:
        return PendingFill(source_bar_close_ms=source_bar.bar_close_ms, fill_model=fill_model)

    # NEXT_BAR_OPEN: fill at the next bar's open, recorded at that bar's close
    # (deterministic — the fill is known once the next bar completes).
    return ExecutionRow(
        ts_ms=next_bar.bar_close_ms,
        exec_id=_shadow_exec_id(strategy_instance_id, source_bar.bar_close_ms, intended.action),
        perm_id=0,  # shadow has no broker perm_id
        client_order_id=_shadow_client_order_id(
            strategy_instance_id, source_bar.bar_close_ms, intended.action
        ),
        account_id=account_id,
        symbol=intended.symbol,
        fill_quantity=_signed_quantity(intended),
        fill_price=float(next_bar.open),
        fee=math.nan,  # shadow has no recorded broker fee
        execution_source="shadow_sim",
        fill_model=fill_model,
        source_bar_close_ms=source_bar.bar_close_ms,
    )


__all__ = ["SUPPORTED_FILL_MODELS", "PendingFill", "UnknownFillModel", "simulate_shadow_fill"]
