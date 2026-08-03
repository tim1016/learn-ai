"""Catalog projection — the bots-list roster rows (spec §5).

``build_catalog`` composes each ``BotCatalogView`` from a bot's lifecycle
status (``BotStatusView``) and its S0 rollup (``BotRollup``). Rollups are read
from a ``BotRollupCache`` that is bootstrapped once from the journal — never a
full journal scan per request (§15). The projection is a pure function over its
inputs so the router seam test can drive it with journal fixtures.

The ``status_label`` maps the lifecycle phase to the closed status vocabulary
(Working / Off duty / Retired, §5). ``needs_attention`` is the OR of the
rollup's decision-based heuristic and lifecycle-derived attention (a hold, an
unclean duty outcome) — the attention-first sort (§5) reads this flag.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.fills import project_instance_fills
from app.broker.alpaca.clerk.models import OrderJournalEntry
from app.broker.alpaca.clerk.rollup_cache import BotRollup, BotRollupCache
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import BotCatalogView

# Phase → the closed status label (§5). RUNNING/STOPPED desired-state overlays
# the phase for the "Working" label so a stopped-but-on-duty bot reads honestly.
_STATUS_LABEL_WORKING = "Working"
_STATUS_LABEL_OFF_DUTY = "Off duty"
_STATUS_LABEL_RETIRED = "Retired"


def status_label_for(status: BotStatusView) -> str:
    """Map a bot's phase + liveness to the closed status vocabulary (§5)."""
    if status.phase == "RETIRED":
        return _STATUS_LABEL_RETIRED
    if status.running:
        return _STATUS_LABEL_WORKING
    return _STATUS_LABEL_OFF_DUTY


def _lifecycle_needs_attention(status: BotStatusView) -> bool:
    """True when the lifecycle itself flags attention (§5).

    An unclean terminal exit (CRASHED / EXITED_UNVERIFIED) needs an operator's
    eye even though the rollup's decision heuristic knows nothing about it.
    """
    outcome = status.duty_outcome
    return outcome is not None and outcome.kind in ("CRASHED", "EXITED_UNVERIFIED")


def status_explanation_for(status: BotStatusView, rollup: BotRollup) -> str:
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


def bootstrap_rollup_cache(
    cache: BotRollupCache,
    sids: list[str],
    entries: list[OrderJournalEntry],
) -> None:
    """Bootstrap ``cache`` from the journal for each bot (cold-start path, §15).

    Reads the journal once and projects attributed fills per bot — the only
    O(journal) work, done at bootstrap, not per request. After this the cache
    serves O(1) catalog reads.
    """
    for sid in sids:
        fills = list(project_instance_fills(sid, entries))
        cache.bootstrap_from_fills(sid, fills)


def compose_catalog_view(
    status: BotStatusView,
    rollup: BotRollup,
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
        last_activity_at_ms=rollup.last_activity_at_ms,
        needs_attention=rollup.needs_attention or _lifecycle_needs_attention(status),
    )


def build_catalog(
    statuses: list[BotStatusView],
    cache: BotRollupCache,
    *,
    account_id: str,
    mark_prices: dict[str, dict[str, float]] | None = None,
) -> list[BotCatalogView]:
    """Build the full catalog, attention-first (§5).

    ``cache`` must already be bootstrapped (call :func:`bootstrap_rollup_cache`).
    Rows are sorted attention-first, then by last activity (most recent first),
    then by sid for a stable order.
    """
    snapshots = cache.snapshot_all(mark_prices=mark_prices)
    views: list[BotCatalogView] = []
    for status in statuses:
        sid = status.strategy_instance_id
        rollup = snapshots.get(sid)
        if rollup is None:
            # A bot with no journal activity yet — synthesise an empty rollup so
            # the roster is complete (an off-duty bot with zero fills is normal).
            rollup = cache.get_rollup(sid)
        views.append(compose_catalog_view(status, rollup, account_id=account_id))

    views.sort(
        key=lambda v: (
            not v.needs_attention,  # attention rows first
            -(v.last_activity_at_ms or 0),  # most recent activity next
            v.strategy_instance_id,  # stable tiebreak
        )
    )
    return views
