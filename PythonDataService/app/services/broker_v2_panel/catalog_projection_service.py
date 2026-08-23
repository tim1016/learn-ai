"""SQLite catalog projection for Broker V2 roster rows (spec §5).

The ``status_label`` maps the lifecycle phase to the closed status vocabulary
(Working / Off duty / Retired, §5). ``needs_attention`` is the OR of the
rollup's decision-based heuristic and lifecycle-derived attention (a hold, an
unclean duty outcome) — the attention-first sort (§5) reads this flag.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite.economic_projection import EconomicSnapshot
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import BotCatalogView

# Phase → the closed status label (§5). RUNNING/STOPPED desired-state overlays
# the phase for the "Working" label so a stopped-but-on-duty bot reads honestly.
_STATUS_LABEL_WORKING = "Working"
_STATUS_LABEL_OFF_DUTY = "Off duty"
_STATUS_LABEL_RETIRED = "Retired"


class SqliteCatalogProjectionUnavailable(RuntimeError):
    """Activated SQLite roster cannot be projected from complete authority facts."""


class SqliteCatalogRevisionMismatch(SqliteCatalogProjectionUnavailable):
    """Custody and economic SQLite readers observed different revisions."""


@dataclass(frozen=True)
class CatalogEconomicRollup:
    sid: str
    exposure: dict[str, float]
    fills_today: int | None
    realized_pnl_today: float | None
    open_pnl: float | None
    last_activity_at_ms: int | None
    needs_attention: bool


def status_label_for(status: BotStatusView) -> str:
    """Map a bot's phase + liveness to the closed status vocabulary (§5)."""
    if status.phase == "RETIRED":
        return _STATUS_LABEL_RETIRED
    if status.running:
        return _STATUS_LABEL_WORKING
    return _STATUS_LABEL_OFF_DUTY


def sqlite_catalog_rollup(snapshot: EconomicSnapshot) -> CatalogEconomicRollup:
    """Adapt one S2 economic snapshot to the roster presentation contract.

    All execution quantities and P&L values are direct S2 projection outputs;
    this adapter intentionally does not re-derive a total from fills.
    """
    return CatalogEconomicRollup(
        sid=snapshot.strategy_instance_id,
        exposure=dict(snapshot.exposure),
        fills_today=snapshot.fills_today,
        realized_pnl_today=snapshot.realized_pnl_today,
        open_pnl=snapshot.open_pnl,
        last_activity_at_ms=snapshot.last_activity_at_ms,
        needs_attention=snapshot.execution_coverage != "complete",
    )


def require_sqlite_catalog_identity(status: BotStatusView) -> BotStatusView:
    """Reject a roster row whose immutable SQLite config identity is absent.

    ``strategy_key`` is populated from the S1 ``bot_config`` row before this
    projection runs.  ``unknown`` was a transitional placeholder that is not a
    valid product identity once SQLite is active.
    """
    if (
        not status.strategy_key
        or status.strategy_key == "unknown"
        or not status.strategy_label
    ):
        raise SqliteCatalogProjectionUnavailable(
            f"Bot '{status.strategy_instance_id}' has no immutable SQLite configuration."
        )
    return status


def _strategy_label_for(status: BotStatusView) -> str:
    """Return a backend-authored label; SQLite rows keep their persisted name."""
    return status.strategy_label or status.strategy_key.replace("_", " ").replace("-", " ").title()


def _lifecycle_needs_attention(status: BotStatusView) -> bool:
    """True when the lifecycle itself flags attention (§5).

    An unclean terminal exit (CRASHED / EXITED_UNVERIFIED) needs an operator's
    eye even though the rollup's decision heuristic knows nothing about it.
    """
    outcome = status.duty_outcome
    return outcome is not None and outcome.kind in ("CRASHED", "EXITED_UNVERIFIED")


def status_explanation_for(status: BotStatusView, rollup: CatalogEconomicRollup) -> str:
    """Author one concise trader-facing explanation for the roster row."""
    if _lifecycle_needs_attention(status):
        return "The previous run ended without verified custody."
    if rollup.needs_attention:
        return "The latest strategy decision is blocked."
    if status.phase == "RETIRED":
        return "Retired; no further runs can start."
    if status.running:
        if status.desired_state == "PAUSED":
            return "Paused; the current run remains live while bar evaluation is held."
        if status.mode == "trade":
            return "Running under Account Clerk custody."
        if status.mode == "dry_run":
            return "Running as a Dry Run; decisions and fills are simulated with no broker writes."
        return "Running in log-only mode; no order custody is active."
    if rollup.exposure:
        return "Off duty with Clerk-attributed exposure."
    return "Off duty and flat."


def day_pnl(realized: float | None, open_pnl: float | None) -> float | None:
    """Null-safe ``realized + open`` — the one day-P&L authority.

    Formula: day_pnl = realized_pnl_today + open_pnl, treating one absent
      component as zero and returning None iff both are absent.
    Reference: docs/superpowers/specs/2026-08-14-bot-gallery-redesign-design.md
      section 3.4; component economics follow docs/references/broker-v2-fifo-pnl.md.
    Canonical implementation: this file.
    Validated against:
      tests/services/test_gallery_hub.py::test_day_pnl_null_safe_projection.

    ``None`` only when both components are unavailable; a lone-present
    component contributes its own value (mirrors the "show whichever side is
    present" display intent this replaces the frontend's own summing of).
    """
    if realized is None and open_pnl is None:
        return None
    # Explicit None-checks, not `x or 0.0` — a legitimate 0.0 P&L is falsy
    # too and must not be confused with "absent".
    return (realized if realized is not None else 0.0) + (
        open_pnl if open_pnl is not None else 0.0
    )


def compose_catalog_view(
    status: BotStatusView,
    rollup: CatalogEconomicRollup,
    *,
    account_id: str,
) -> BotCatalogView:
    """Compose one roster row from a bot's status and its rollup (§5).

    The roster preserves the backend-owned desired state, including ``PAUSED``
    for a live run whose evaluation is temporarily held.
    """
    return BotCatalogView(
        strategy_instance_id=status.strategy_instance_id,
        strategy_key=status.strategy_key,
        strategy_label=_strategy_label_for(status),
        broker=status.broker,
        account_id=account_id,
        symbol=status.symbol,
        mode=status.mode,
        phase=status.phase,
        desired_state=status.desired_state,
        running=status.running,
        status_label=status_label_for(status),
        status_explanation=status_explanation_for(status, rollup),
        exposure=dict(rollup.exposure),
        fills_today=rollup.fills_today,
        realized_pnl_today=rollup.realized_pnl_today,
        open_pnl=rollup.open_pnl,
        day_pnl=day_pnl(rollup.realized_pnl_today, rollup.open_pnl),
        last_activity_at_ms=rollup.last_activity_at_ms,
        needs_attention=rollup.needs_attention or _lifecycle_needs_attention(status),
    )
