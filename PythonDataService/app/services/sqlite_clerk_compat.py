"""Compatibility projections for surfaces retained across SQLite cutover."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from app.broker.alpaca.clerk.account_authority import (
    account_route_matches_custody,
    authority_kind_in_world,
    custody_account_id_for,
    is_shadow_account_id,
    is_synthetic_account_id,
)
from app.broker.alpaca.clerk.active_authority import (
    custody_world_or_paper,
    get_active_clerk_runtime,
    primary_custody_world,
)
from app.broker.alpaca.clerk.active_runtime import SQLITE_FACADE_AUTHORITIES
from app.broker.alpaca.clerk.models import ChannelHealth, ClerkStatus
from app.broker.alpaca.clerk.sqlite.account_eligibility import (
    AccountEligibilityCondition,
    account_eligibility_condition,
)
from app.broker.alpaca.clerk.sqlite.order_projection import (
    ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES,
)
from app.broker.alpaca.clerk.sqlite.projection_models import (
    ClerkProjection,
    ProjectedOperation,
)
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.reconciliation_sweep import ReconciliationSweep
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    RecoveryPolicyContext,
    build_projection_guidance,
    build_recovery_catalog,
)
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.models import BrokerAccountSnapshot
from app.broker.v2panel.vocabulary import copy_for, hold_reason_for
from app.schemas.account_authority import CustodyWorld
from app.schemas.clerk_custody import CustodyDiagnosis
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


def active_sqlite_facade(broker: str = "alpaca") -> SqliteAlpacaClerkFacade | None:
    """Return the selected Alpaca Broker V2 authority, not host-runner health.

    The host-runner Clerk inventory this warned against
    (``HostRunnerHealth.clerks``, a process listing that could name a Clerk for
    another account) retired with the IBKR control plane in #1813. The rule it
    motivated stands: Alpaca status and custody decisions resolve through this
    selector so that another broker's Clerk is never mistaken for the Alpaca
    SQLite authority.
    """
    if broker != "alpaca":
        return None
    runtime = get_active_clerk_runtime()
    if (
        runtime is None
        or runtime.authority_kind not in SQLITE_FACADE_AUTHORITIES
        or not isinstance(runtime.clerk, SqliteAlpacaClerkFacade)
    ):
        return None
    return runtime.clerk


def active_reconciliation_sweep(broker: str = "alpaca") -> ReconciliationSweep | None:
    """Return the running ADR 0050 sweep behind the active SQLite authority.

    Gated identically to :func:`active_sqlite_facade` -- same broker and
    authority-kind checks -- so a caller reaching for
    ``ReconciliationSweep.revive_now()`` (the write path's supervised lease
    revival) always finds the one sweep already running the account's lease
    heartbeat, instead of constructing a second one. ``runtime.sweep`` is
    typed narrowly as ``BackgroundSweep`` (start/stop only) at its
    construction site; the ``isinstance`` check here is what recovers the
    concrete type the revival entry point lives on.
    """
    if broker != "alpaca":
        return None
    runtime = get_active_clerk_runtime()
    if (
        runtime is None
        or runtime.authority_kind not in SQLITE_FACADE_AUTHORITIES
        or not isinstance(runtime.clerk, SqliteAlpacaClerkFacade)
        or not isinstance(runtime.sweep, ReconciliationSweep)
    ):
        return None
    return runtime.sweep


def _labelled_custody_world() -> CustodyWorld:
    """This process's primary world, labelled for a compat surface.

    The one place this module coerces an absent authority to the paper label
    (``custody_world_or_paper``). Written once so two surfaces of the same
    request cannot answer with two different worlds, and so a reader looking
    for "where does this module decide the world?" finds one answer.
    """
    return custody_world_or_paper(primary_custody_world())


def custody_account_id_for_route(broker: str, resolved: str) -> str:
    """The custody id a route's resolved account id names on the active authority.

    Every account-scoped route resolves the *broker's* account id; the SQLite
    readers key on the id the active authority **custodies**. Those are the
    same id in the real-paper world and deliberately different under shadow,
    where custody is ``shadow:<live_account_id>`` (ADR 0059 D2) -- so an
    untranslated route id fails the readers' account guard on every correct
    shadow boot.

    Translation, never a bypass: the guard it feeds still refuses a foreign
    account, because a foreign id translates to a foreign custody id. Only the
    Alpaca authority has a custody world, so another broker's id is returned
    unchanged rather than relabelled with this one's.
    """
    if broker != "alpaca":
        return resolved
    return custody_account_id_for(_labelled_custody_world(), resolved)


def lane_account_id_for_seal(broker: str, sealed_account_id: str) -> str | None:
    """The custody account the account roster lists a sealed bot under.

    A real or ``shadow:`` seal names its own custody account. A Dry Run seal
    names the bot's isolated ``sim:<strategy_instance_id>`` authority, which no
    public route names: the roster (``panel_data_source.get_catalog``) lists
    it under this lane's primary authority, so it resolves to that
    authority's account. ``None`` when no primary SQLite authority is active,
    the state the roster answers as unavailable.
    """
    if not is_synthetic_account_id(sealed_account_id):
        return sealed_account_id
    facade = active_sqlite_facade(broker)
    return None if facade is None else facade.account_id


def sqlite_projection(
    *,
    account_id: str,
    strategy_instance_id: str | None,
) -> ClerkProjection | None:
    facade = active_sqlite_facade()
    if facade is None:
        return None
    if facade.account_id != account_id:
        raise ValueError("Requested account is not the active SQLite authority")
    reader = SqliteClerkProjectionReader.from_facade(facade)
    try:
        projection = (
            reader.account_snapshot()
            if strategy_instance_id is None
            else reader.bot_snapshot(strategy_instance_id)
        )
    finally:
        reader.close()
    if projection is None:
        raise ValueError("Requested bot is not registered with the SQLite authority")
    return projection


def failed_sqlite_projection(
    *,
    account_id: str,
    strategy_instance_id: str | None,
) -> ClerkProjection | None:
    """Expose typed account-wide impact when an activated authority fails boot.

    This projection deliberately contains no fabricated durable state. Offline
    rebuild/reset capabilities are present but unavailable until their external
    proof is verified after the service process is stopped.
    """
    runtime = get_active_clerk_runtime()
    if runtime is None or runtime.authority_kind != "unavailable":
        return None
    failure = runtime.startup_failure
    if (
        failure is None
        or not failure.activation_detected
        or failure.account_id is None
        or not account_route_matches_custody(
            account_id, failure.account_id, shadow=is_shadow_account_id(failure.account_id),
        )
    ):
        return None
    # The projection names the failed authority's custody id, as a healthy
    # snapshot does, whatever the route's spelling.
    custody_account_id = failure.account_id
    now_ms = now_ms_utc()
    authority_generation = failure.authority_generation or 0
    db_identity_token = failure.db_identity_token or "unverified-activation"
    context = RecoveryPolicyContext(
        account_id=custody_account_id,
        strategy_instance_id=strategy_instance_id,
        authority_generation=authority_generation,
        db_identity_token=db_identity_token,
        authority_health="failed",
        authority_health_reason=failure.recovery,
        control_revision=0,
        now_ms=now_ms,
        runs=(),
        current_orders=(),
        positions=(),
        uncertainties=(),
        latest_account_reconciliation=None,
    )
    recovery_actions = build_recovery_catalog(context)
    return ClerkProjection(
        account_id=custody_account_id,
        strategy_instance_id=strategy_instance_id,
        authority_generation=authority_generation,
        db_identity_token=db_identity_token,
        authority_health="failed",
        authority_health_reason=failure.recovery,
        control_revision=0,
        custody_owner="ACCOUNT_CLERK",
        runs=(),
        commands=(),
        operations=(),
        working_order_refs=(),
        positions=(),
        holds=(),
        uncertainties=(),
        latest_reconciliation=None,
        terminal_receipts=(),
        guidance=build_projection_guidance(context, recovery_actions),
        recovery_actions=recovery_actions,
        generated_at_ms=now_ms,
    )


def account_eligibility(
    projection: ClerkProjection, account: BrokerAccountSnapshot,
) -> AccountEligibilityCondition | None:
    """Whether Alpaca lets this custody's account trade in its world (#1664).

    Judged on one account observation. An observation naming a different
    account than ``projection`` is an explicit identity mismatch, never a
    blend of two accounts' facts. "Different account" is asked of the
    *custody* id the world implies, not of the broker's own answer: a shadow
    authority observing ``9LIVE0001`` legitimately custodies
    ``shadow:9LIVE0001`` (ADR 0059 D2).
    """
    custody_world = _labelled_custody_world()
    identity_mismatch = custody_account_id_for(custody_world, account.account_id) != projection.account_id
    if identity_mismatch:
        logger.warning(
            "An account read named a different account than the active projection; "
            "reporting an identity mismatch",
            extra={
                "action": "account_identity_mismatch",
                "projection_account_id": projection.account_id,
                "observed_account_id": account.account_id,
                "custody_world": custody_world,
            },
        )
    return account_eligibility_condition(
        account, custody_world=custody_world, identity_mismatch=identity_mismatch,
    )


def sqlite_clerk_status(
    projection: ClerkProjection,
    *,
    channel_healths: Sequence[ChannelHealth] | None = None,
) -> ClerkStatus:
    """Compose the Clerk status: the hold, the latest reconciliation verdict,
    the commands still resolving, and the submission-gate channels."""
    hold = projection.holds[0] if projection.holds else None
    unresolved = sum(
        _operation_requires_reconciliation(operation)
        for operation in projection.operations
    )
    latest = projection.latest_reconciliation
    if projection.uncertainties:
        verdict = "stale"
    elif hold is not None:
        verdict = "unexplained_order"
    else:
        verdict = "clean"
    return ClerkStatus(
        broker="alpaca",
        account_id=projection.account_id,
        hold={
            "active": hold is not None,
            "reason_code": hold.reason_code if hold is not None else None,
            # The hold's own cause, not the projection's guidance: guidance is
            # authored from uncertainties and can read "healthy" beside a hold.
            "reason": (
                copy_for(hold_reason_for(active=True, stored_code=hold.reason_code)).explanation
                if hold is not None
                else None
            ),
            "since_ms": hold.opened_at_ms if hold is not None else None,
        },
        latest_reconciliation=(
            {"verdict": verdict, "recorded_at_ms": latest.attempted_at_ms}
            if latest is not None
            else None
        ),
        outstanding_intents=unresolved,
        observed_at_ms=projection.generated_at_ms,
        channel_healths=(
            list(channel_healths) if channel_healths is not None else None
        ),
        # Derived from the id and the primary's world, never asserted (slice 4 / slice 7, R12).
        authority_kind=authority_kind_in_world(projection.account_id, _labelled_custody_world()),
    )


def _operation_requires_reconciliation(operation: ProjectedOperation) -> bool:
    """Mirror the SQLite authority's broker-facing nonterminal predicate."""
    if operation.state in {"succeeded", "failed", "rejected"}:
        return False
    if operation.state in {"accepted", "unknown"} or operation.kind == "EXIT":
        return True
    return not operation.orders or any(
        order.broker_state is None
        or order.broker_state.lower() not in ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES
        or (order.broker_state.lower() == "filled" and order.filled_quantity == 0)
        for order in operation.orders
    )


def sqlite_custody_diagnosis(projection: ClerkProjection) -> CustodyDiagnosis:
    divergent = bool(projection.holds or projection.uncertainties)
    evidence_refs = tuple(
        dict.fromkeys(
            reference
            for item in (*projection.holds, *projection.uncertainties)
            for reference in item.evidence_refs
        )
    )
    divergences = ()
    if divergent:
        divergences = (
            {
                "kind": "needs_review",
                "state": "needs_review",
                "explanation": projection.guidance.explanation,
                "possible_causes": (projection.guidance.impact,),
                "prerequisite_detail": projection.guidance.next_step,
                "evidence_refs": evidence_refs,
            },
        )
    return CustodyDiagnosis(
        broker="alpaca",
        account_id=projection.account_id,
        in_sync=not divergent,
        observed_at_ms=projection.generated_at_ms,
        snapshot_version=(
            f"sqlite:{projection.authority_generation}:{projection.control_revision}:"
            f"{projection.db_identity_token}"
        ),
        resolvable=False,
        blocked_reason=(
            None
            if not divergent
            else "Use the SQLite Clerk's typed, evidence-bound recovery actions."
        ),
        authority_kind=authority_kind_in_world(projection.account_id, _labelled_custody_world()),
        divergences=divergences,
        resolution_plan=(),
    )
