"""The audit read surface (#2104): ``GET /api/broker-clerks/audit/routing-receipts``.

The durable routing-receipt trail was append-only, trigger-protected, and
reachable only by opening the coordinator's SQLite file by hand. This is the
one read-only, secret-gated, coordinator-only route that converts that into
a curl-able surface — no idempotency key, no command envelope, no new
storage, no ceremony change.

Covers, per ``.claude/rules/temporal-rigor.md`` and ``.claude/rules/
numerical-rigor.md``:

- the store's ``since_ms`` inclusive lower bound, proved in both directions
  (reddens if the bound became exclusive; reddens if it were dropped);
- the secret gate, proved in both directions (with header -> 200, without
  -> refused);
- the 503-when-uninstalled path, proved against a working positive control;
- that every timestamp on the wire is ``int64 ms UTC`` -- no ISO string, no
  ``datetime`` object, anywhere in the response;
- ``capability``, resolved read-time from the live provider catalog by
  ``(broker, operation_kind)`` -- proved to be a real many-to-one lookup
  (not a hardcoded value, not ``operation_kind`` echoed back), and proved to
  report ``None`` rather than raise or guess for an operation the current
  catalog no longer declares.

The fake catalogs in ``conftest.py`` deliberately give every operation an id
that differs from its capability's value (``read_account`` vs.
``account_read``, ``submit_bot_action`` vs. ``bot_action``) -- a fixture
whose id and capability happen to read the same cannot catch a bug where one
field is substituted for the other.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.broker.fleet.errors import DataPlaneControlSecretRefused, FleetControlPlaneNotInstalled
from app.broker.fleet.provider import (
    Capability,
    OperationIdempotency,
    OperationReadiness,
    ProviderOperation,
)
from app.broker.fleet.records import RoutingReceiptRecord, RoutingReceiptState
from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from app.config import settings
from app.routers import broker_clerks
from app.security.data_plane_control import CONTROL_SECRET_HEADER
from app.utils.error_handlers import install_fleet_control_error_handler
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from tests.broker.fleet.conftest import (
    FakeProviderAdapter,
    FrozenClock,
    Lane,
    bind_lane,
    provision_lane,
)

ROUTE = "/api/broker-clerks/audit/routing-receipts"
_TEST_SECRET = "test-audit-secret"
_ISO_LIKE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")

#: The renamed fake-alpha bot-action operation (conftest.py) -- its id
#: deliberately differs from its capability's value ("bot_action").
_RESOLVABLE_OPERATION_KIND = "submit_bot_action"


def _coordinator_app(fleet_service: FleetControlService | None) -> FastAPI:
    """The minimal coordinator surface this read needs: no lane router, no agent."""
    app = FastAPI()
    if fleet_service is not None:
        app.state.fleet_service = fleet_service
    install_fleet_control_error_handler(app)
    app.include_router(broker_clerks.router)
    return app


def _open_and_settle(
    fleet_service: FleetControlService,
    lane: Lane,
    *,
    key: str,
    target: str = "strategy/sid-audit",
    operation_kind: str = _RESOLVABLE_OPERATION_KIND,
    outcome: RoutingReceiptState = RoutingReceiptState.DELIVERED,
) -> RoutingReceiptRecord:
    """Open, dispatch and settle one routing attempt; return the settled receipt."""
    attempt = fleet_service.open_routing_attempt(
        broker=lane.broker,
        clerk_id=lane.clerk_id,
        operation_kind=operation_kind,
        nonsecret_target_ref=target,
        idempotency_key=key,
        pinned_routing_epoch=1,
        pinned_binding_generation=1,
        pinned_agent_instance_id="agnt_111111111111111111111111",
    )
    fleet_service.mark_routing_dispatched(correlation_id=attempt.correlation_id)
    upstream_ref = "upstream/r-1" if outcome == RoutingReceiptState.DELIVERED else None
    return fleet_service.settle_routing_attempt(
        correlation_id=attempt.correlation_id, outcome=outcome, upstream_receipt_ref=upstream_ref
    )


#: A dedicated two-operation, one-capability catalog: real many-to-one
#: evidence (the reviewed live Alpaca catalog collapses 76 operations into 12
#: capabilities; ``custody_command`` alone covers 10). Two *different*
#: operation ids share one capability so a hardcoded or hand-wavy lookup
#: cannot coincidentally pass -- only a genuine per-operation catalog scan
#: reports the same capability for both.
_CUSTODY_OPERATIONS = frozenset(
    {
        ProviderOperation(
            operation_id="reconcile_custody",
            method="POST",
            path_template="/custody/reconcile",
            agent_path_template="/api/fake-custody/custody/reconcile",
            capability=Capability.CUSTODY_COMMAND,
            readiness=OperationReadiness.EXECUTION,
            requires_effective_account=True,
            idempotency=OperationIdempotency.ONE_SHOT,
        ),
        ProviderOperation(
            operation_id="release_custody_hold",
            method="POST",
            path_template="/custody/release-hold",
            agent_path_template="/api/fake-custody/custody/release-hold",
            capability=Capability.CUSTODY_COMMAND,
            readiness=OperationReadiness.EXECUTION,
            requires_effective_account=True,
            idempotency=OperationIdempotency.ONE_SHOT,
        ),
        ProviderOperation(
            operation_id="read_positions_snapshot",
            method="GET",
            path_template="/positions",
            agent_path_template="/api/fake-custody/positions",
            capability=Capability.POSITIONS_READ,
            readiness=OperationReadiness.EXECUTION,
            requires_effective_account=False,
            idempotency=OperationIdempotency.READ,
        ),
    }
)


def _custody_adapter() -> FakeProviderAdapter:
    """A fake provider whose catalog collapses two operations into one
    capability -- the many-to-one shape this audit read must resolve."""
    return FakeProviderAdapter(
        provider_id="fake_custody",
        capabilities=frozenset({Capability.CUSTODY_COMMAND, Capability.POSITIONS_READ}),
        declared_operations=_CUSTODY_OPERATIONS,
        canonical_rule=lambda raw: raw.strip().upper(),
    )


# ---- store: since_ms is an inclusive lower bound, proved both directions --


def test_store_since_ms_is_inclusive_of_the_boundary(
    control_dir: Path, fleet_service: FleetControlService, clock: FrozenClock
) -> None:
    """A receipt created exactly at ``since_ms`` is included.

    Reddens if the store's comparison were made exclusive (``>`` instead of
    the contractually-required ``>=``).
    """
    lane = provision_lane(fleet_service, broker="fake_alpha", label="bound", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-bound")

    clock.advance(1_000)
    boundary = clock()
    at_boundary = _open_and_settle(fleet_service, lane, key="bound-at")
    assert at_boundary.created_at_ms == boundary  # the fixture's own premise

    receipts = fleet_service._store.list_routing_receipts(clerk_id=lane.clerk_id, since_ms=boundary)

    assert at_boundary.correlation_id in {r.correlation_id for r in receipts}


def test_store_since_ms_excludes_receipts_strictly_before_the_bound(
    control_dir: Path, fleet_service: FleetControlService, clock: FrozenClock
) -> None:
    """A receipt created strictly before ``since_ms`` is excluded.

    Reddens if the ``since_ms`` filter were dropped entirely (the older
    receipt would then appear). The unfiltered read is asserted non-empty
    and to carry both receipts first, so this is not a vacuous pass against
    a store that returns nothing for unrelated reasons.
    """
    lane = provision_lane(fleet_service, broker="fake_alpha", label="exclude", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-exclude")

    before = _open_and_settle(fleet_service, lane, key="exclude-before")
    clock.advance(1_000)
    boundary = clock()
    at_boundary = _open_and_settle(fleet_service, lane, key="exclude-at")

    # Positive control: both receipts really exist before any since_ms filter
    # is applied at all.
    unfiltered = fleet_service._store.list_routing_receipts(clerk_id=lane.clerk_id)
    assert {r.correlation_id for r in unfiltered} == {before.correlation_id, at_boundary.correlation_id}

    receipts = fleet_service._store.list_routing_receipts(clerk_id=lane.clerk_id, since_ms=boundary)
    ids = {r.correlation_id for r in receipts}

    assert before.correlation_id not in ids
    assert at_boundary.correlation_id in ids


# ---- service: the response envelope carries exactly the audit's fields ----


def test_service_list_routing_receipts_envelope_shape(
    control_dir: Path, fleet_service: FleetControlService, clock: FrozenClock
) -> None:
    lane = provision_lane(fleet_service, broker="fake_alpha", label="shape", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-shape")
    settled = _open_and_settle(fleet_service, lane, key="shape-1")

    result = fleet_service.list_routing_receipts(since_ms=0, clerk_id=lane.clerk_id)

    assert set(result) == {
        "observed_at_ms",
        "receipts",
        "has_more",
        "next_before_ms",
        "next_before_correlation_id",
    }
    assert isinstance(result["observed_at_ms"], int)
    assert result["has_more"] is False
    assert result["next_before_ms"] is None
    assert result["next_before_correlation_id"] is None
    assert len(result["receipts"]) == 1
    entry = result["receipts"][0]
    assert set(entry) == {
        "correlation_id",
        "clerk_id",
        "broker",
        "operation_kind",
        "capability",
        "routing_state",
        "nonsecret_target_ref",
        "idempotency_key",
        "upstream_receipt_ref",
        "pinned_routing_epoch",
        "pinned_binding_generation",
        "pinned_agent_instance_id",
        "created_at_ms",
        "dispatched_at_ms",
        "updated_at_ms",
    }
    assert entry["correlation_id"] == settled.correlation_id
    assert entry["clerk_id"] == lane.clerk_id
    assert entry["broker"] == lane.broker
    assert entry["operation_kind"] == _RESOLVABLE_OPERATION_KIND
    assert entry["capability"] == "bot_action"
    assert entry["routing_state"] == "delivered"
    assert entry["nonsecret_target_ref"] == settled.nonsecret_target_ref
    assert entry["idempotency_key"] == settled.idempotency_key
    assert entry["upstream_receipt_ref"] == settled.upstream_receipt_ref
    assert entry["pinned_routing_epoch"] == settled.pinned_routing_epoch
    assert entry["pinned_binding_generation"] == settled.pinned_binding_generation
    assert entry["pinned_agent_instance_id"] == settled.pinned_agent_instance_id
    assert entry["created_at_ms"] == settled.created_at_ms
    assert entry["dispatched_at_ms"] == settled.dispatched_at_ms
    assert entry["updated_at_ms"] == settled.updated_at_ms


def test_service_projects_reconciliation_identity_for_an_outcome_unknown_receipt(
    control_dir: Path, fleet_service: FleetControlService, clock: FrozenClock
) -> None:
    """An ``outcome_unknown`` receipt -- the state whose own contract (#2133
    P1-b, ``records.py``) says it "must be reconciled by identity, never
    resubmitted blindly" -- carries every field that identity requires: the
    pinned attempt context (epoch, binding generation, agent instance), the
    caller's idempotency key and target, and (correctly, for this outcome)
    no upstream receipt reference yet.
    """
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="reconcile", tmp_path=control_dir.parent
    )
    bind_lane(fleet_service, lane, account="acct-reconcile")
    settled = _open_and_settle(
        fleet_service,
        lane,
        key="reconcile-1",
        target="strategy/sid-reconcile",
        outcome=RoutingReceiptState.OUTCOME_UNKNOWN,
    )
    assert settled.upstream_receipt_ref is None  # the fixture's own premise

    result = fleet_service.list_routing_receipts(since_ms=0, clerk_id=lane.clerk_id)

    entry = result["receipts"][0]
    assert entry["routing_state"] == "outcome_unknown"
    assert entry["nonsecret_target_ref"] == "strategy/sid-reconcile"
    assert entry["idempotency_key"] == "reconcile-1"
    assert entry["upstream_receipt_ref"] is None
    assert entry["pinned_routing_epoch"] == 1
    assert entry["pinned_binding_generation"] == 1
    assert entry["pinned_agent_instance_id"] == "agnt_111111111111111111111111"


# ---- service: capability is resolved read-time, not hardcoded, not the ----
# ---- operation_kind echoed back, and genuinely many-to-one -----------------


def test_capability_is_resolved_per_operation_not_a_constant_or_echo(
    control_dir: Path, fleet_service: FleetControlService, clock: FrozenClock
) -> None:
    """Two different operations on the same lane resolve to two different,
    correct capabilities.

    Reddens if the lookup returned a hardcoded constant (both would then
    report that one constant, at most one correct). Reddens if the lookup
    echoed ``operation_kind`` back unchanged (the fake catalog's ids
    deliberately differ from their capability values, so an echo would fail
    both assertions below).
    """
    lane = provision_lane(fleet_service, broker="fake_alpha", label="per-op", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-per-op")

    account_read = _open_and_settle(
        fleet_service, lane, key="per-op-account", operation_kind="read_account"
    )
    bot_action = _open_and_settle(
        fleet_service, lane, key="per-op-bot", operation_kind="submit_bot_action"
    )

    result = fleet_service.list_routing_receipts(since_ms=0, clerk_id=lane.clerk_id)
    by_id = {entry["correlation_id"]: entry for entry in result["receipts"]}

    assert by_id[account_read.correlation_id]["capability"] == "account_read"
    assert by_id[bot_action.correlation_id]["capability"] == "bot_action"


def test_capability_is_genuinely_many_to_one_across_two_operation_ids(
    control_dir: Path, clock: FrozenClock
) -> None:
    """Two *different* operation ids that share one capability both report
    that same capability.

    A test using a single operation cannot tell a real per-operation catalog
    lookup from a hardcoded string; this one can only pass if the lookup
    actually scans the catalog for each receipt independently, since
    ``reconcile_custody`` and ``release_custody_hold`` are unrelated strings
    that happen to share ``custody_command`` -- exactly the shape the live
    Alpaca catalog exhibits (10 operation ids under ``custody_command``).
    """
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=control_dir),
        provider_adapters={"fake_custody": _custody_adapter()},
        clock=clock,
    )
    try:
        lane = provision_lane(
            service, broker="fake_custody", label="many-to-one", tmp_path=control_dir.parent
        )
        bind_lane(service, lane, account="acct-many-to-one")

        reconcile = _open_and_settle(
            service, lane, key="m2o-reconcile", operation_kind="reconcile_custody"
        )
        release = _open_and_settle(
            service, lane, key="m2o-release", operation_kind="release_custody_hold"
        )

        result = service.list_routing_receipts(since_ms=0, clerk_id=lane.clerk_id)
        by_id = {entry["correlation_id"]: entry for entry in result["receipts"]}

        assert reconcile.operation_kind != release.operation_kind  # the fixture's own premise
        assert by_id[reconcile.correlation_id]["capability"] == "custody_command"
        assert by_id[release.correlation_id]["capability"] == "custody_command"
    finally:
        service.close()


def test_capability_is_none_for_an_operation_kind_the_catalog_no_longer_declares(
    control_dir: Path, fleet_service: FleetControlService, clock: FrozenClock, caplog: pytest.LogCaptureFixture
) -> None:
    """A receipt whose ``operation_kind`` is retired or pre-dates a rename
    reports ``capability: null`` -- never raises, never guesses.

    The unfiltered read is asserted first (positive control) so this is not
    a vacuous pass against a read that silently returns nothing.
    """
    lane = provision_lane(fleet_service, broker="fake_alpha", label="retired", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-retired")
    settled = _open_and_settle(
        fleet_service, lane, key="retired-1", operation_kind="operation_retired_2019"
    )

    with caplog.at_level("WARNING", logger="app.broker.fleet.service"):
        result = fleet_service.list_routing_receipts(since_ms=0, clerk_id=lane.clerk_id)

    assert result["receipts"], "positive control: the unfiltered read is not empty"
    entry = result["receipts"][0]
    assert entry["correlation_id"] == settled.correlation_id
    assert entry["operation_kind"] == "operation_retired_2019"
    assert entry["capability"] is None
    assert any(
        record.__dict__.get("action") == "audit_capability_unresolved" for record in caplog.records
    ), "the unresolved capability is logged loudly, not silently swallowed"


def test_capability_unresolved_warning_is_deduplicated_per_broker_operation_kind_per_request(
    control_dir: Path, fleet_service: FleetControlService, clock: FrozenClock, caplog: pytest.LogCaptureFixture
) -> None:
    """A page of receipts from one retired operation logs the unresolved
    warning *once*, not once per receipt.

    Five receipts share one retired ``operation_kind``; a sixth carries a
    second, distinct retired ``operation_kind`` on the same lane, proving the
    dedup key is ``(broker, operation_kind)`` and not a single "already
    warned this request" flag that would wrongly silence the second one too.
    Reddens if the per-request dedup set were dropped: a 500-row page from a
    single retired operation would then emit up to 500 warnings per poll,
    forever -- exactly the historical case this branch exists to support.
    """
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="flood", tmp_path=control_dir.parent
    )
    bind_lane(fleet_service, lane, account="acct-flood")
    for i in range(5):
        _open_and_settle(
            fleet_service, lane, key=f"flood-{i}", operation_kind="operation_retired_a"
        )
    _open_and_settle(fleet_service, lane, key="flood-other", operation_kind="operation_retired_b")

    with caplog.at_level("WARNING", logger="app.broker.fleet.service"):
        result = fleet_service.list_routing_receipts(since_ms=0, clerk_id=lane.clerk_id)

    assert len(result["receipts"]) == 6, "positive control: all six receipts are in the page"
    assert {entry["capability"] for entry in result["receipts"]} == {None}

    unresolved_warnings = [
        record
        for record in caplog.records
        if record.__dict__.get("action") == "audit_capability_unresolved"
    ]
    warned_kinds = {record.__dict__.get("operation_kind") for record in unresolved_warnings}
    assert warned_kinds == {"operation_retired_a", "operation_retired_b"}
    assert len(unresolved_warnings) == 2, (
        f"expected exactly one warning per distinct (broker, operation_kind), got "
        f"{len(unresolved_warnings)}: {[r.__dict__.get('operation_kind') for r in unresolved_warnings]}"
    )


# ---- service: clerk scoping is an exclusion, and limit keeps the newest ----


def test_service_clerk_id_excludes_another_lanes_receipts(
    control_dir: Path, fleet_service: FleetControlService, clock: FrozenClock
) -> None:
    """A clerk-scoped read must not carry another lane's receipts.

    Proving the requested lane's receipt is *present* proves nothing about
    scoping -- an unscoped read contains it too. Only asserting the other
    lane's receipt is *absent* discriminates, so this reddens if the
    ``clerk_id`` argument were dropped on the way to the store and every
    lane's correlation ids leaked to a caller who scoped their request.
    """
    mine = provision_lane(fleet_service, broker="fake_alpha", label="scope-mine", tmp_path=control_dir.parent)
    bind_lane(fleet_service, mine, account="acct-scope-mine")
    theirs = provision_lane(
        fleet_service, broker="fake_alpha", label="scope-theirs", tmp_path=control_dir.parent
    )
    bind_lane(fleet_service, theirs, account="acct-scope-theirs")
    assert mine.clerk_id != theirs.clerk_id  # the fixture's own premise

    my_receipt = _open_and_settle(fleet_service, mine, key="scope-mine-1")
    their_receipt = _open_and_settle(fleet_service, theirs, key="scope-theirs-1")

    # Positive control: unscoped, the store really holds both receipts, so an
    # empty or broken store cannot make the exclusion below pass vacuously.
    unscoped = fleet_service.list_routing_receipts(since_ms=0)
    assert {entry["correlation_id"] for entry in unscoped["receipts"]} == {
        my_receipt.correlation_id,
        their_receipt.correlation_id,
    }

    scoped = fleet_service.list_routing_receipts(since_ms=0, clerk_id=mine.clerk_id)
    ids = {entry["correlation_id"] for entry in scoped["receipts"]}

    assert my_receipt.correlation_id in ids
    assert their_receipt.correlation_id not in ids
    assert {entry["clerk_id"] for entry in scoped["receipts"]} == {mine.clerk_id}


def test_service_limit_returns_the_newest_receipts_not_the_oldest(
    control_dir: Path, fleet_service: FleetControlService, clock: FrozenClock
) -> None:
    """Under a truncating ``limit`` the audit returns the *newest* receipts.

    The store, service and route docstrings all promise "newest first", but
    a set-equality assertion over an untruncated read passes just as well
    for ascending order. Only a ``limit`` below the available count can tell
    the two apart, so this reddens if the ``ORDER BY`` were flipped -- the
    oldest two would come back for a caller asking "what happened most
    recently".
    """
    lane = provision_lane(fleet_service, broker="fake_alpha", label="order", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-order")

    oldest = _open_and_settle(fleet_service, lane, key="order-1")
    clock.advance(1_000)
    middle = _open_and_settle(fleet_service, lane, key="order-2")
    clock.advance(1_000)
    newest = _open_and_settle(fleet_service, lane, key="order-3")
    assert oldest.created_at_ms < middle.created_at_ms < newest.created_at_ms

    truncated = fleet_service.list_routing_receipts(since_ms=0, clerk_id=lane.clerk_id, limit=2)
    ids = [entry["correlation_id"] for entry in truncated["receipts"]]

    assert ids == [newest.correlation_id, middle.correlation_id]
    assert oldest.correlation_id not in ids
    stamps = [entry["created_at_ms"] for entry in truncated["receipts"]]
    assert stamps == sorted(stamps, reverse=True)


# ---- store/service: keyset pagination reaches the full window past limit --


def test_store_before_ms_and_before_correlation_id_seek_past_a_shared_timestamp(
    control_dir: Path, fleet_service: FleetControlService, clock: FrozenClock
) -> None:
    """Two receipts share one ``created_at_ms`` (the clock is not advanced
    between them). Seeking before the pair's higher ``correlation_id``
    returns exactly the lower one -- never both, never neither.

    Reddens if the ``correlation_id`` tiebreak were dropped from the
    predicate (comparing on ``created_at_ms`` alone): an exclusive bound on
    the shared timestamp would then return neither receipt, not just the
    lower one.
    """
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="tiebreak", tmp_path=control_dir.parent
    )
    bind_lane(fleet_service, lane, account="acct-tiebreak")

    first = _open_and_settle(fleet_service, lane, key="tie-1")
    second = _open_and_settle(fleet_service, lane, key="tie-2")
    assert first.created_at_ms == second.created_at_ms  # the fixture's own premise
    assert first.correlation_id != second.correlation_id

    higher, lower = sorted([first, second], key=lambda r: r.correlation_id, reverse=True)

    receipts = fleet_service._store.list_routing_receipts(
        clerk_id=lane.clerk_id,
        before_ms=higher.created_at_ms,
        before_correlation_id=higher.correlation_id,
    )

    assert {r.correlation_id for r in receipts} == {lower.correlation_id}


def test_store_before_ms_requires_before_correlation_id(
    control_dir: Path, fleet_service: FleetControlService
) -> None:
    """The two seek parameters are a pair; one without the other is a bug at
    the call site, not a silently-ignored bound."""
    with pytest.raises(ValueError, match="before_ms and before_correlation_id"):
        fleet_service._store.list_routing_receipts(before_ms=1_000)
    with pytest.raises(ValueError, match="before_ms and before_correlation_id"):
        fleet_service._store.list_routing_receipts(before_correlation_id="corr_x")


def test_service_pagination_walks_the_full_window_with_duplicate_timestamps(
    control_dir: Path, fleet_service: FleetControlService, clock: FrozenClock
) -> None:
    """Seed more receipts than ``limit`` -- including two that share one
    ``created_at_ms`` -- then page with the returned continuation values
    until ``has_more`` is false. The union of every page must be exactly the
    seeded set: every receipt once, none twice, none missing.

    This is the audit-window-reachability deliverable (#2133 P1-a): before
    this pagination existed, a caller asking for more than ``limit`` receipts
    had no way to reach the rest, and lowering ``since_ms`` alone only
    re-selected the same newest rows. It also reddens if the
    ``correlation_id`` tiebreak were dropped from the store predicate -- the
    duplicate-timestamp pair is seeded first so it is the oldest pair and is
    guaranteed to straddle a page boundary at the ``limit`` values exercised
    below, which a timestamp-only bound would skip or repeat.
    """
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="walk", tmp_path=control_dir.parent
    )
    bind_lane(fleet_service, lane, account="acct-walk")

    seeded = [
        _open_and_settle(fleet_service, lane, key="walk-dup-1"),
        _open_and_settle(fleet_service, lane, key="walk-dup-2"),
    ]
    assert seeded[0].created_at_ms == seeded[1].created_at_ms  # the fixture's own premise
    for i in range(5):
        clock.advance(1_000)
        seeded.append(_open_and_settle(fleet_service, lane, key=f"walk-{i}"))
    assert len(seeded) == 7
    seeded_ids = {receipt.correlation_id for receipt in seeded}

    for limit in (2, 3):
        collected: list[str] = []
        before_ms: int | None = None
        before_correlation_id: str | None = None
        pages = 0
        while True:
            pages += 1
            assert pages <= 20, f"limit={limit}: pagination did not terminate"
            result = fleet_service.list_routing_receipts(
                since_ms=0,
                clerk_id=lane.clerk_id,
                limit=limit,
                before_ms=before_ms,
                before_correlation_id=before_correlation_id,
            )
            collected.extend(entry["correlation_id"] for entry in result["receipts"])
            if not result["has_more"]:
                assert result["next_before_ms"] is None
                assert result["next_before_correlation_id"] is None
                break
            before_ms = result["next_before_ms"]
            before_correlation_id = result["next_before_correlation_id"]

        assert len(collected) == len(seeded_ids), f"limit={limit}: wrong total count"
        assert len(collected) == len(set(collected)), f"limit={limit}: a receipt repeated"
        assert set(collected) == seeded_ids, f"limit={limit}: union missed the seeded set"


# ---- HTTP: the secret gate, proved in both directions ---------------------


@pytest.mark.asyncio
async def test_http_rejects_the_request_without_the_secret_header(
    control_dir: Path, fleet_service: FleetControlService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    lane = provision_lane(fleet_service, broker="fake_alpha", label="gate", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-gate")
    _open_and_settle(fleet_service, lane, key="gate-1")
    app = _coordinator_app(fleet_service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(ROUTE, params={"since_ms": 0})

    assert response.status_code == 403
    body = response.json()
    assert body["reason"] == DataPlaneControlSecretRefused.reason
    assert CONTROL_SECRET_HEADER in body["message"]
    assert "detail" not in body
    assert set(body).issubset({"reason", "message", "next_step"})


@pytest.mark.asyncio
async def test_http_accepts_the_request_with_the_correct_secret_header(
    control_dir: Path, fleet_service: FleetControlService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    lane = provision_lane(fleet_service, broker="fake_alpha", label="gate-ok", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-gate-ok")
    settled = _open_and_settle(fleet_service, lane, key="gate-ok-1")
    app = _coordinator_app(fleet_service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            ROUTE,
            params={"since_ms": 0, "clerk_id": lane.clerk_id},
            headers={CONTROL_SECRET_HEADER: _TEST_SECRET},
        )

    assert response.status_code == 200
    body = response.json()
    assert [r["correlation_id"] for r in body["receipts"]] == [settled.correlation_id]


@pytest.mark.asyncio
async def test_http_rejects_the_wrong_secret_header(
    control_dir: Path, fleet_service: FleetControlService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    app = _coordinator_app(fleet_service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            ROUTE, params={"since_ms": 0}, headers={CONTROL_SECRET_HEADER: "wrong"}
        )

    assert response.status_code == 403
    assert response.json()["reason"] == DataPlaneControlSecretRefused.reason


# ---- HTTP: coordinator-only, 503 when no fleet_service is installed -------


@pytest.mark.asyncio
async def test_http_503s_when_fleet_service_is_absent_not_500_and_not_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-coordinator process (no ``app.state.fleet_service``) refuses
    with the existing 503 family -- never a raw 500, never a silent empty
    200."""
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    app = _coordinator_app(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            ROUTE, params={"since_ms": 0}, headers={CONTROL_SECRET_HEADER: _TEST_SECRET}
        )

    assert response.status_code == 503
    body = response.json()
    assert body["reason"] == FleetControlPlaneNotInstalled.reason
    assert "detail" not in body
    assert "receipts" not in body


@pytest.mark.asyncio
async def test_http_serves_receipts_when_fleet_service_is_installed_positive_control(
    control_dir: Path, fleet_service: FleetControlService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control for the 503 test above: the identical request against
    an otherwise-identical app that *does* carry ``fleet_service`` succeeds,
    so the 503 is proven to come from the missing service, not from some
    other misconfiguration shared by both apps."""
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    lane = provision_lane(fleet_service, broker="fake_alpha", label="present", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-present")
    _open_and_settle(fleet_service, lane, key="present-1")
    app = _coordinator_app(fleet_service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            ROUTE, params={"since_ms": 0}, headers={CONTROL_SECRET_HEADER: _TEST_SECRET}
        )

    assert response.status_code == 200
    assert response.json()["receipts"]


# ---- HTTP: temporal rigor — int64 ms UTC only, nothing else on the wire ---


@pytest.mark.asyncio
async def test_http_response_carries_only_int64_ms_utc_timestamps(
    control_dir: Path, fleet_service: FleetControlService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    lane = provision_lane(fleet_service, broker="fake_alpha", label="temporal", tmp_path=control_dir.parent)
    bind_lane(fleet_service, lane, account="acct-temporal")
    _open_and_settle(fleet_service, lane, key="temporal-1")
    app = _coordinator_app(fleet_service)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            ROUTE, params={"since_ms": 0}, headers={CONTROL_SECRET_HEADER: _TEST_SECRET}
        )

    assert response.status_code == 200
    assert not _ISO_LIKE.search(response.text), "no ISO-8601 instant may reach the wire"
    body = response.json()
    assert body["receipts"], "positive control: the response actually carries receipts"
    assert isinstance(body["observed_at_ms"], int)
    for entry in body["receipts"]:
        assert isinstance(entry["created_at_ms"], int)
        assert isinstance(entry["updated_at_ms"], int)
        assert entry["dispatched_at_ms"] is None or isinstance(entry["dispatched_at_ms"], int)


@pytest.mark.asyncio
async def test_http_since_ms_bound_is_le_max_timestamp_ms(
    control_dir: Path, fleet_service: FleetControlService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``since_ms`` accepts the domain ceiling and refuses one past it --
    never ``2**63 - 1``, which would state a wire ceiling higher than what
    this route actually enforces."""
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    app = _coordinator_app(fleet_service)
    headers = {CONTROL_SECRET_HEADER: _TEST_SECRET}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        at_ceiling = await client.get(ROUTE, params={"since_ms": MAX_TIMESTAMP_MS}, headers=headers)
        past_ceiling = await client.get(
            ROUTE, params={"since_ms": MAX_TIMESTAMP_MS + 1}, headers=headers
        )
        negative = await client.get(ROUTE, params={"since_ms": -1}, headers=headers)
        missing = await client.get(ROUTE, headers=headers)

    assert at_ceiling.status_code == 200
    assert past_ceiling.status_code == 422
    assert negative.status_code == 422
    assert missing.status_code == 422


@pytest.mark.asyncio
async def test_http_limit_is_bounded_one_to_five_hundred(
    control_dir: Path, fleet_service: FleetControlService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    app = _coordinator_app(fleet_service)
    headers = {CONTROL_SECRET_HEADER: _TEST_SECRET}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        zero = await client.get(ROUTE, params={"since_ms": 0, "limit": 0}, headers=headers)
        too_many = await client.get(ROUTE, params={"since_ms": 0, "limit": 501}, headers=headers)
        max_ok = await client.get(ROUTE, params={"since_ms": 0, "limit": 500}, headers=headers)

    assert zero.status_code == 422
    assert too_many.status_code == 422
    assert max_ok.status_code == 200


# ---- HTTP: keyset pagination round-trips over the wire ---------------------


@pytest.mark.asyncio
async def test_http_before_ms_and_before_correlation_id_round_trip_a_second_page(
    control_dir: Path,
    fleet_service: FleetControlService,
    clock: FrozenClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truncated page's ``next_before_ms``/``next_before_correlation_id``,
    fed back as ``before_ms``/``before_correlation_id``, reaches the receipt
    the first page could not."""
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    lane = provision_lane(
        fleet_service, broker="fake_alpha", label="http-page", tmp_path=control_dir.parent
    )
    bind_lane(fleet_service, lane, account="acct-http-page")
    older = _open_and_settle(fleet_service, lane, key="http-page-older")
    clock.advance(1_000)
    newer = _open_and_settle(fleet_service, lane, key="http-page-newer")
    app = _coordinator_app(fleet_service)
    headers = {CONTROL_SECRET_HEADER: _TEST_SECRET}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get(
            ROUTE, params={"since_ms": 0, "clerk_id": lane.clerk_id, "limit": 1}, headers=headers
        )
        assert first.status_code == 200
        first_body = first.json()
        assert [r["correlation_id"] for r in first_body["receipts"]] == [newer.correlation_id]
        assert first_body["has_more"] is True

        second = await client.get(
            ROUTE,
            params={
                "since_ms": 0,
                "clerk_id": lane.clerk_id,
                "limit": 1,
                "before_ms": first_body["next_before_ms"],
                "before_correlation_id": first_body["next_before_correlation_id"],
            },
            headers=headers,
        )

    assert second.status_code == 200
    second_body = second.json()
    assert [r["correlation_id"] for r in second_body["receipts"]] == [older.correlation_id]
    assert second_body["has_more"] is False


@pytest.mark.asyncio
async def test_http_before_ms_without_before_correlation_id_is_refused(
    control_dir: Path, fleet_service: FleetControlService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seek pair cannot be given half-supplied over the wire either."""
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    app = _coordinator_app(fleet_service)
    headers = {CONTROL_SECRET_HEADER: _TEST_SECRET}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            ROUTE, params={"since_ms": 0, "before_ms": 1_000}, headers=headers
        )

    assert response.status_code == 422
