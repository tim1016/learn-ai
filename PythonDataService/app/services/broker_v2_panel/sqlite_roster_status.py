"""Project one activated SQLite bot into a roster ``BotStatusView``.

Extracted from ``sqlite_panel_source`` so the roster row -- immutable
identity, declared submission capability, durable terminal outcome -- has one
home instead of being a third responsibility inside the panel seam.

Two facts this module refuses to guess, because guessing either misstates
what a bot may do or hides what a bot already did:

* **Declared configuration.** Mode, quantity, and carryover policy come from
  SQLite's own ``config_json``; a row missing them is refused, never
  defaulted (FR-031).
* **Terminal outcome.** Receipt-first, projection-second -- see
  :func:`terminal_duty_outcome` for why it is that order and not either one
  alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from app.broker.alpaca.clerk.sqlite.models import BotConfigResource
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.ibkr.config import live_artifacts_root
from app.engine.live.bot_lifecycle_state import (
    BotLifecycleStateCorruptError,
    BotLifecycleStateRecord,
    BotLifecycleStateRepo,
    stable_bot_lifecycle_state_path,
)
from app.schemas.broker_bots import BotDutyOutcomeView, BotStatusView
from app.services.bot_binding_repository import live_state_binding_repository
from app.services.bot_registry_projection import (
    duty_outcome_view,
    duty_outcome_view_from_receipt,
)
from app.services.broker_v2_panel.catalog_projection_service import (
    SqliteCatalogProjectionUnavailable,
)


@dataclass(frozen=True)
class DeclaredConfiguration:
    """The submission-capability facts a roster row must state truthfully."""

    mode: Literal["log_only", "dry_run", "trade"]
    quantity: int | None
    carryover_policy: Literal["FORBID", "ALLOW"]


def declared_configuration(strategy_instance_id: str, config_json: str) -> DeclaredConfiguration:
    """Read mode, quantity, and carryover policy out of SQLite's own config.

    Refuses rather than substitutes a default: guessing `trade` for a row
    whose real mode is unreadable would overstate what the bot may do, and
    guessing `log_only` would understate it. Neither is safe to render.
    """
    try:
        declared = json.loads(config_json)
    except json.JSONDecodeError as exc:
        raise SqliteCatalogProjectionUnavailable(
            f"Bot '{strategy_instance_id}' has unreadable immutable SQLite configuration."
        ) from exc
    mode = declared.get("mode")
    carryover_policy = declared.get("carryover_policy")
    if mode not in ("log_only", "dry_run", "trade") or carryover_policy not in ("FORBID", "ALLOW"):
        raise SqliteCatalogProjectionUnavailable(
            f"Bot '{strategy_instance_id}' has no declared mode or carryover policy in SQLite."
        )
    quantity = declared.get("quantity")
    return DeclaredConfiguration(
        mode=mode,
        quantity=int(quantity) if quantity is not None else None,
        carryover_policy=carryover_policy,
    )


def lifecycle_record(strategy_instance_id: str) -> BotLifecycleStateRecord | None:
    """Read the bot's durable lifecycle record for duty-outcome projection.

    An absent record (legacy bots) projects None; a corrupt one refuses the
    projection loudly, matching this module's incomplete-config contract --
    silently projecting ``duty_outcome=None`` here is what hid a crashed
    fleet behind "Off duty" (fleet-stress T6, 2026-08-26).
    """
    path = stable_bot_lifecycle_state_path(live_artifacts_root(), strategy_instance_id)
    try:
        return BotLifecycleStateRepo(path).read()
    except BotLifecycleStateCorruptError as exc:
        raise SqliteCatalogProjectionUnavailable(
            f"Bot '{strategy_instance_id}' has an unreadable lifecycle projection: {exc}"
        ) from exc


def terminal_duty_outcome(
    strategy_instance_id: str,
    lifecycle: BotLifecycleStateRecord | None,
    repository: ClerkSqliteRepository,
    *,
    running: bool,
) -> BotDutyOutcomeView | None:
    """Project this bot's terminal outcome: receipt-first, projection-second.

    ``run_outcomes/{run_id}.json`` is create-once terminal authority;
    ``lifecycle_state.json`` is a lower-fidelity summary of it that
    ``BotRunEvidenceService.record_terminal`` writes *after* the receipt. A
    projection write that fails after the receipt lands therefore leaves a
    proven terminal fact with nothing to render it -- the roster shows an
    innocent "Off duty" over a durable crash, which is exactly the T6
    failure class.

    The projection is not a decorative fallback, though, and the receipt is
    not universal. The boot sweep records the fleet's most common terminal
    outcome -- ``EXITED_UNVERIFIED`` / ``INTERRUPTED_BY_RESTART`` -- through
    ``AlpacaLifecycleProjector.project_terminal`` alone and writes **no**
    receipt (``bot_boot_recovery``), and a provisional stop is deliberately
    projected with ``persist_receipt=False``. Reading only receipts would
    blank exactly the rows T6 restored. So the projection answers whenever it
    has an answer, and the receipt is consulted only where it does not.

    A running bot has no terminal outcome to look for, so neither the SQLite
    run lookup nor the receipt read happens on the fleet's hot path.

    Known asymmetry: the legacy registry producer
    (``bot_registry_projection.project_bot_status``) still projects from the
    lifecycle record alone. It is a pure function over already-read
    artifacts, so giving it the same precedence means threading a binding
    repository through it -- deliberately left for a follow-up rather than
    done in passing. Both producers share the two canonical mappings
    (:func:`duty_outcome_view`, :func:`duty_outcome_view_from_receipt`), so
    the shape of the fact stays single-sourced either way.
    """
    projected = duty_outcome_view(lifecycle)
    if projected is not None or running:
        return projected
    latest = repository.latest_run(strategy_instance_id)
    if latest is None or latest.state == "ACTIVE":
        return None
    try:
        receipt = live_state_binding_repository(live_artifacts_root()).read_outcome(
            strategy_instance_id, latest.lifecycle_run_id
        )
    except (OSError, ValueError) as exc:
        raise SqliteCatalogProjectionUnavailable(
            f"Bot '{strategy_instance_id}' has an unreadable terminal receipt: {exc}"
        ) from exc
    return duty_outcome_view_from_receipt(receipt)


@dataclass(frozen=True)
class RosterMembership:
    """The catalog's membership list, split by how much work each row needs."""

    identities: list[str]
    """Every registration, in registration order."""

    inert_terminal: frozenset[str]
    """Retired registrations with no live custody — see :func:`roster_membership`."""

    @property
    def with_live_custody(self) -> list[str]:
        """The identities whose rows must be projected in full, in order."""
        return [
            strategy_instance_id
            for strategy_instance_id in self.identities
            if strategy_instance_id not in self.inert_terminal
        ]


def roster_membership(repository: ClerkSqliteRepository) -> RosterMembership:
    """Return the activated roster's identities, split by required work.

    The catalog needs the membership list before it can read projections for
    it. Reading identities alone -- rather than building throwaway status rows
    and keeping only their ids -- is what keeps a catalog request to one
    lifecycle-file read per bot instead of two.

    A registration is *inert terminal* when it is retired and the one batched
    custody query says nothing bot-scoped is outstanding against it. Those rows
    need neither a custody projection nor a lifecycle file read: retirement
    fixes every field they carry, and read cost is otherwise linear in a row
    count that only ever grows (#1911). Everything else -- including a retired
    bot that still holds custody -- is projected in full, so #1778's authored
    cure for a stranded retired bot still reaches the roster.
    """
    registrations = repository.strategy_instances()
    live_custody = repository.strategy_instances_with_live_custody()
    identities: list[str] = []
    inert_terminal: set[str] = set()
    for registration in registrations:
        strategy_instance_id = str(registration["strategy_instance_id"])
        identities.append(strategy_instance_id)
        if (
            registration.get("retired_at_ms") is not None
            and strategy_instance_id not in live_custody
        ):
            inert_terminal.add(strategy_instance_id)
    return RosterMembership(
        identities=identities,
        inert_terminal=frozenset(inert_terminal),
    )


def _declared_identity(
    registration: dict[str, object],
    repository: ClerkSqliteRepository,
) -> tuple[str, BotConfigResource, DeclaredConfiguration]:
    """The immutable SQLite identity every roster row is composed from.

    FR-031: the roster's immutable configuration is read from SQLite, not
    assumed. Mode, quantity and carryover policy were previously hardcoded to
    `trade` / `None` / `FORBID`, and `panel_data_source._status_in_binding_mode`
    only patched them back for `dry_run` -- so a `log_only` bot, which must
    never submit an order, rendered on the roster as `trade`, and a bot
    deployed with `ALLOW` carryover rendered as `FORBID`. Both misstate what a
    bot may do with real money's paper stand-in. `config_json` carries the real
    values for every row this projection accepts; a row missing them cannot be
    projected honestly and is refused like any other incomplete config.
    """
    strategy_instance_id = str(registration["strategy_instance_id"])
    config = repository.bot_config(strategy_instance_id)
    if (
        config is None
        or config.strategy_instance_id != strategy_instance_id
        or not config.strategy_key
        or config.strategy_key == "unknown"
        or not config.display_name
    ):
        raise SqliteCatalogProjectionUnavailable(
            f"Bot '{strategy_instance_id}' has no immutable SQLite configuration."
        )
    return (
        strategy_instance_id,
        config,
        declared_configuration(strategy_instance_id, config.config_json),
    )


def build_terminal_roster_status(
    broker: str,
    registration: dict[str, object],
    repository: ClerkSqliteRepository,
) -> BotStatusView:
    """Compose a retired row from SQLite identity alone (#1911).

    A retired registration can never admit another run -- ``run_admission``
    refuses ``BOT_RETIRED`` -- so every field below is fixed by the fact of
    retirement, and the per-row work the live builder does to discover them is
    work with a known answer. That matters because the catalog pays it once
    per roster row on every poll, and retired rows only ever accumulate.

    Callers must have proved the registration inert first (see
    ``roster_rows_with_live_custody``): a retired bot that still holds custody
    keeps its authored cure and takes the live path, because that is the case
    #1778 exists for.

    ``duty_outcome`` is deliberately ``None`` rather than the terminal outcome
    of the bot's last run: retiring is an operator stating they have dealt with
    this registration, which resolves how its final run ended. Live custody is
    what can still need attention on a retired bot, and that reaches the row
    through the custody projection, not through this field.
    """
    strategy_instance_id, config, declared = _declared_identity(registration, repository)
    retired_at_ms = registration.get("retired_at_ms")
    if retired_at_ms is None:
        raise SqliteCatalogProjectionUnavailable(
            f"Bot '{strategy_instance_id}' is not retired and has no terminal roster row."
        )
    return BotStatusView(
        strategy_instance_id=strategy_instance_id,
        strategy_key=config.strategy_key,
        strategy_label=config.display_name,
        broker=broker,
        symbol=str(registration["symbol"]),
        mode=declared.mode,
        quantity=declared.quantity,
        carryover_policy=declared.carryover_policy,
        running=False,
        phase="RETIRED",
        desired_state="STOPPED",
        active_run_id=None,
        duty_outcome=None,
        binding_created_at_ms=int(registration["created_at_ms"]),
        last_transition_at_ms=int(retired_at_ms),
    )


def build_roster_status(
    broker: str,
    registration: dict[str, object],
    repository: ClerkSqliteRepository,
) -> BotStatusView:
    """Compose one roster row from SQLite identity plus durable lifecycle."""
    strategy_instance_id, config, declared = _declared_identity(registration, repository)
    active_run = repository.active_run(strategy_instance_id)
    retired_at_ms = registration.get("retired_at_ms")
    running = active_run is not None
    lifecycle = lifecycle_record(strategy_instance_id)
    return BotStatusView(
        strategy_instance_id=strategy_instance_id,
        strategy_key=config.strategy_key,
        strategy_label=config.display_name,
        broker=broker,
        symbol=str(registration["symbol"]),
        mode=declared.mode,
        quantity=declared.quantity,
        carryover_policy=declared.carryover_policy,
        running=running,
        phase=("RETIRED" if retired_at_ms is not None else "ON_DUTY" if running else "OFF_DUTY"),
        desired_state="RUNNING" if running else "STOPPED",
        active_run_id=(str(active_run.lifecycle_run_id) if active_run is not None else None),
        duty_outcome=terminal_duty_outcome(
            strategy_instance_id,
            lifecycle,
            repository,
            running=running,
        ),
        binding_created_at_ms=int(registration["created_at_ms"]),
        last_transition_at_ms=(
            int(retired_at_ms)
            if retired_at_ms is not None
            else int(active_run.started_at_ms)
            if active_run is not None
            else None
        ),
    )


__all__ = [
    "DeclaredConfiguration",
    "RosterMembership",
    "build_roster_status",
    "build_terminal_roster_status",
    "declared_configuration",
    "lifecycle_record",
    "roster_membership",
    "terminal_duty_outcome",
]
