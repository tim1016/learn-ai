"""SQLite catalog projection for Broker V2 roster rows.

The ``status_label`` maps the lifecycle phase to the closed status vocabulary
(Working / Off duty / Retired). ``needs_attention`` is the OR of the
rollup's decision-based heuristic and lifecycle-derived attention (a hold, an
unclean duty outcome) — the attention-first sort reads this flag.
``group`` places the row on the account's Home (PRD #2560 D5/D7) and
``world_label`` names the world it trades in, worded once.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.sqlite.economic_projection import EconomicSnapshot
from app.broker.v2panel.vocabulary import copy_for, duty_outcome_copy_key
from app.schemas.account_authority import AuthorityKind
from app.schemas.bot_history import BotHistoryStatus
from app.schemas.bot_lifecycle import UNCLEAN_DUTY_OUTCOMES
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import BotCatalogView, BotGroup
from app.services.broker_v2_panel.panel_errors import DryRunRestoringError, PanelUnavailableError

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

# Phase → the closed status label. RUNNING/STOPPED desired-state overlays
# the phase for the "Working" label so a stopped-but-on-duty bot reads honestly.
_STATUS_LABEL_WORKING = "Working"
_STATUS_LABEL_OFF_DUTY = "Off duty"
_STATUS_LABEL_RETIRED = "Retired"


#: The one wording of each world a bot can trade in (PRD #2560 D4). Dry Run
#: is its own world: simulated cash, never the account's money.
WORLD_LABELS: dict[AuthorityKind, str] = {
    "real_live": "LIVE · real money",
    "real_paper": "PAPER · practice money",
    "shadow": "SHADOW · simulated fills on your live account",
    "synthetic": "DRY RUN · simulated cash",
}


def bot_world(authority_world: AuthorityKind, mode: str) -> AuthorityKind:
    """The world one bot trades in: its authority's, or simulated for a Dry Run.

    A bot configured to simulate trades simulated cash whatever account it
    sits under (PRD #2560 D4/D5), so its group and its label both follow
    this one answer and can never disagree (H23).
    """
    return "synthetic" if mode == "dry_run" else authority_world


def bot_group(*, world: AuthorityKind, running: bool, holds_money: bool) -> BotGroup:
    """Where a bot sits on its account's Home (PRD #2560 D5/D7).

    ``world`` is the bot's own (``bot_world``). A bot is running while it
    runs; once stopped it is holding while it has position cost or
    still-claimed money (``holds_money``), and finished when it is flat with
    nothing still claimed. Nothing is cleared by hand while it holds: a
    holding bot moves to finished by itself once its money is released.

    A Dry Run trades simulated cash, never the account's (D5), so while it
    runs or still holds simulated money it keeps its own Dry Run group, out
    of the bar's running and holding groups. Once stopped and flat it is
    finished like any other bot -- worded by its world label, "DRY RUN ·
    simulated cash" -- so the Finished fold is the one place a bot is
    cleared from (owner decision 2026-09-28, #2567).
    """
    if running or holds_money:
        if world == "synthetic":
            return "dry_run"
        return "running" if running else "holding"
    return "finished"


def bot_status(*, retired: bool, live_custody: bool, running: bool, holds_money: bool) -> BotHistoryStatus:
    """Where a bot is now: the one answer History's rows and the bot's own page give (#2574).

    Cleared is the catalog's inert terminal row (``roster_membership``):
    retired, with nothing bot-scoped outstanding (``live_custody`` is
    ``strategy_instances_with_live_custody``). Otherwise ``bot_group``'s rule,
    without Dry Run's own Home group -- a Dry Run's world is its own column
    there, not its status: running while it runs, holding while it holds
    money (``bots_holding_money``), and finished once flat.
    """
    if retired and not live_custody:
        return "cleared"
    if running:
        return "running"
    return "holding" if holds_money else "finished"


def custody_bot_status(repository: ClerkSqliteRepository, strategy_instance_id: str, *, running: bool) -> BotHistoryStatus:
    """``bot_status`` for one bot, from its own custody file's facts."""
    registration = repository.strategy_instance(strategy_instance_id)
    return bot_status(
        retired=registration is not None and registration.get("retired_at_ms") is not None,
        live_custody=strategy_instance_id in repository.strategy_instances_with_live_custody(),
        running=running,
        holds_money=strategy_instance_id in repository.bots_holding_money(),
    )


def ended_at_ms(status: BotStatusView, *, latest_stop_ms: int | None) -> int | None:
    """When a stopped bot's run ended; ``None`` while it runs or never ran (``run_ended_at_ms``)."""
    return run_ended_at_ms(
        running=status.running,
        stop_ms=latest_stop_ms,
        outcome_recorded_at_ms=None if status.duty_outcome is None else status.duty_outcome.recorded_at_ms,
        retired_at_ms=status.last_transition_at_ms if status.phase == "RETIRED" else None,
    )


def run_ended_at_ms(
    *, running: bool, stop_ms: int | None, outcome_recorded_at_ms: int | None, retired_at_ms: int | None,
) -> int | None:
    """When a run ended; ``None`` while it runs or never ran.

    The run's own stop instant is the answer. A run that ended without one
    -- a crash leaves its run row open -- ended when its duty outcome was
    recorded, and a retirement with no run ended when it retired.
    """
    if running:
        return None
    if stop_ms is not None:
        return stop_ms
    if outcome_recorded_at_ms is not None:
        return outcome_recorded_at_ms
    return retired_at_ms


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


#: The closed-vocabulary labels for a Dry Run whose own simulator cannot be
#: read right now (#2684): its own row on Home, its facts unknown.
_STATUS_LABEL_RESTORING = "Restoring"
_STATUS_LABEL_UNAVAILABLE = "Unavailable"


def unreadable_catalog_view(
    status: BotStatusView, *, account_id: str, failure: PanelUnavailableError
) -> BotCatalogView:
    """The roster row for a Dry Run whose own simulator cannot be read right now (#2684).

    Boot may still be restoring it, or its account may be held elsewhere:
    either way no economic fact is readable, so every rollup is unknown and
    exposure is ``None`` -- never ``{}``, which would read as flat. The bot
    is never dropped from Home and never fails the lane's roster (#2582),
    and it stays in the Dry Run group: "finished" would claim a flatness
    nobody can prove. The row says why, in the failure's own words; only a
    failure that is not a restoration asks for attention.
    """
    restoring = isinstance(failure, DryRunRestoringError)
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
        status_label=_STATUS_LABEL_RESTORING if restoring else _STATUS_LABEL_UNAVAILABLE,
        status_explanation=str(failure) if failure.detail is None else f"{failure} {failure.detail}",
        exposure=None,
        fills_today=None,
        realized_pnl_today=None,
        open_pnl=None,
        day_pnl=None,
        last_activity_at_ms=None,
        needs_attention=not restoring,
        group="dry_run",
        world_label=WORLD_LABELS["synthetic"],
    )


def status_label_for(status: BotStatusView) -> str:
    """Map a bot's phase + liveness to the closed status vocabulary.

    An unclean terminal outcome overrides "Off duty" (S3b): a crashed run
    must never read the same as a deliberate stop. The label is the shared
    vocabulary's own copy for that outcome, so the roster and the health
    card name a crash identically.
    """
    if status.phase == "RETIRED":
        return _STATUS_LABEL_RETIRED
    if status.running:
        return _STATUS_LABEL_WORKING
    outcome = status.duty_outcome
    if outcome is not None and _lifecycle_needs_attention(status):
        return copy_for(duty_outcome_copy_key(outcome.kind)).label
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
    """True when the lifecycle itself flags attention.

    An unclean terminal exit (CRASHED / EXITED_UNVERIFIED) needs an operator's
    eye even though the rollup's decision heuristic knows nothing about it.
    """
    outcome = status.duty_outcome
    return outcome is not None and outcome.kind in UNCLEAN_DUTY_OUTCOMES


def status_explanation_for(status: BotStatusView, rollup: CatalogEconomicRollup) -> str:
    """Author one concise trader-facing explanation for the roster row."""
    if _lifecycle_needs_attention(status):
        return "The previous run ended without verified custody."
    if rollup.needs_attention:
        return "The latest strategy decision is blocked."
    if status.phase == "RETIRED":
        return "Retired; no further runs can start."
    if status.running:
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
    Reference: none external; component economics follow
      ``app.broker.alpaca.clerk.fifo_pnl``.
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
    world: AuthorityKind,
    holds_money: bool,
    latest_stop_ms: int | None,
) -> BotCatalogView:
    """Compose one roster row from a bot's status and its rollup.

    The roster preserves the backend-owned Running or Stopped intent.
    ``world`` is the row's authority's; the row's own world follows its mode.
    """
    world = bot_world(world, status.mode)
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
        group=bot_group(world=world, running=status.running, holds_money=holds_money),
        world_label=WORLD_LABELS[world],
        ended_at_ms=ended_at_ms(status, latest_stop_ms=latest_stop_ms),
    )
