"""The Alpaca lane's fleet boot: presence, reservation, confirmation.

This module sequences the Alpaca agent's side of the ADR 0062 addendum's
admission protocol around the existing authority boot, without touching any
of the authority's own semantics:

1. ``open_fleet_lane`` — before any profile or custody database opens: the
   volume identity gate, then session registration under a fresh instance id
   and routing epoch. A coordinator that cannot be reached opens the lane
   *offline* only when the volume already carries confirmation evidence
   vouching for a prior grant; a first enrolment with no coordinator refuses.
2. ``reserve_account`` — the broker-qualified reservation, before custody
   and the execution lease open (PRD FR-063). The loser of a reservation
   race refuses to open authority at all.
3. ``confirm_binding`` — after the worker's local acknowledgement: the
   confirmed binding observation fenced by this session, then the clerk's
   durable confirmation evidence.
4. ``start_heartbeat`` — observations only; an observation never confirms
   anything. Its one exception is repair, not protocol: a refused beat
   re-registers the lane, and a replacement session that inherits a granted
   binding re-confirms it (step 3 again) rather than leaving the lane
   projecting ``starting`` until a human restarts the container.
5. While draining, the beat also carries the lane's lane-quiet confirmation
   (#2154, ADR 0063 Decision 2): a separate, session-fenced call made on
   every beat, never a heartbeat field, so the answer the retirement gate
   reads is always a fresh observation rather than a remembered one.

Step 4 is not sequenced with steps 2 and 3 at all: the beat starts when the
lane *opens*, before the installation lock, before the profiles database and
before any binding, and stops when the lane closes. Presence belongs to the
lane, not to its binding. Gating the beat on a binding (as main.py once did)
deadlocked an unbound lane — it registered, then went silent, so the
coordinator projected it ``unreachable`` and refused the
``configuration_access`` reads that were the only way to bind it. An
installation that never produces a binding is a reachable outcome, not an
edge case: the lock can refuse this process, the profiles database can be
unavailable, a profile can be staged and never applied.

So a lane reports ``binding_pending`` with an ``unidentified`` endpoint mode
from the moment it opens, and ``confirm_and_report`` — steps 3 and the facts
half of 4 — confirms the grant if there is one and swaps
``FleetLaneBoot.reported_facts`` under the already-running beat.

Three things belong to the open lane rather than to its binding, for the one
reason: an unbound lane is routable for ``configuration_access``, so it must
behave like a served lane before anything binds it. The beat
(``start_heartbeat``/``stop_heartbeat``) is one; the FR-076 identity echo is
the second, installed beside the beat by ``main.serve_lane_presence``; the
approved endpoint reference (``FleetLaneBoot.endpoint_ref``) is the third,
re-presented on every re-registration so a replacement session still names a
destination the coordinator will deliver to.

With the coordinator unreachable mid-boot, the offline rule is FR-066: only
a recovered binding that exactly matches the confirmation evidence boots
(last-effective recovery); a first assignment or a changed binding waits for
the coordinator, failing closed.

The lane's own drain is learned, never assumed (#2155): a heartbeat's
lifecycle answer, or the coordinator's typed refusal at registration,
re-authors the volume's confirmation evidence into a drained tombstone
(``_learn_drain``), and that file then refuses the FR-066 offline boot that
would otherwise resurrect the binding during the next coordinator outage.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.fleet_adapter import AlpacaProviderAdapter
from app.broker.fleet import volume as volume_module
from app.broker.fleet.confirmation import (
    ConfirmationEvidence,
    confirmation_evidence_path,
    evidence_vouches_for,
    mark_confirmation_evidence_draining,
    read_confirmation_evidence,
    write_confirmation_evidence,
)
from app.broker.fleet.errors import FleetControlError
from app.broker.fleet.identity import new_agent_instance_id
from app.broker.fleet.internal_http import FleetTransportRefused
from app.broker.fleet.presence import (
    FleetLaneDraining,
    FleetPresence,
    FleetPresenceError,
    LocalPresence,
    RemotePresence,
    SessionInfo,
)
from app.broker.fleet.provider import FLEET_PROTOCOL_VERSION
from app.broker.fleet.records import (
    _ACCOUNT_NICKNAME_MAX_CHARS,
    LANE_QUIET_CONDITIONS,
    AccountAssignmentRecord,
    StoredLifecycleState,
)
from app.broker.fleet.service import FleetControlService, FleetRegistryStore
from app.config import FleetSettings
from app.utils.timestamps import now_ms_utc

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.lane_quiet import AccountQuietObservation

logger = logging.getLogger(__name__)

_ADAPTER = AlpacaProviderAdapter()


class FleetBootRefused(FleetControlError):
    """The lane may not open authority under the fleet's admission rules.

    Internal family: boot fails closed and the refusal names the ceremony
    that resolves it.
    """

    reason = "fleet_boot_refused"
    status_code = 409


@dataclass(frozen=True, slots=True)
class ConfirmedGrant:
    """The exact grant one confirmation named, re-presentable to a coordinator.

    ``confirm_assignment`` re-acknowledges an equal binding generation only
    when the effective tuple matches the one that generation already
    confirmed, so a re-confirmation presents all four values or none. They are
    one immutable object rather than four fields precisely because no reader
    may ever see half of them, and no writer may set them apart.
    """

    external_account_id: str
    binding_generation: int
    effective_profile_id: str | None
    effective_revision: int | None


@dataclass(frozen=True, slots=True)
class LaneQuietAnswer:
    """This lane's answer to the four lane-quiet conditions it owns (#2154).

    The fifth, that the lane is draining, is the registry's fact. The field
    names are ``confirm_lane_quiet``'s, so the answer crosses the seam
    unrenamed.
    """

    observed_at_ms: int
    runner_idle: bool
    broker_work_ended: bool
    account_flat: bool
    intents_resolved: bool

    @property
    def outstanding(self) -> tuple[str, ...]:
        """The conditions left unsatisfied, in the registry's declared order."""
        return tuple(
            phrase for name, phrase in LANE_QUIET_CONDITIONS if not getattr(self, name)
        )


#: How long one beat waits for the lane-quiet observation. It runs inline on
#: the beat, and two broker reads against a slow broker could otherwise hold
#: the beat past the coordinator's session staleness (30 s) and project a
#: lane that still holds its account as unreachable. A timed-out observation
#: is no answer, exactly like an unreadable broker.
LANE_QUIET_OBSERVATION_TIMEOUT_S = 5.0

#: Produces a fresh answer, or ``None`` when the lane cannot observe its
#: account this beat — no answer, never a "not quiet" one.
LaneQuietProbe = Callable[[], Awaitable[LaneQuietAnswer | None]]


def lane_quiet_probe(
    *,
    bots_running: Callable[[], bool],
    observe_account: Callable[[], Awaitable[AccountQuietObservation | None]],
) -> LaneQuietProbe:
    """Compose the lane's answer from the bot runner and the account's clerk.

    "No bot running" means the runner's own tasks have exited, not that the
    panel records ``STOPPED``: the panel holds what the operator wants, the
    task registry holds what is still running. It is read before and after
    the account observation and holds only if both reads find nothing, so a
    bot still winding down while the broker was read cannot slip between
    them. Once the lane has learned its drain a new start or resume refuses
    (``bot_runner.drained_lane_start_gate``), which is what keeps the answer
    true after it is taken. The gate is checked at admission, so a start
    already past it when the drain is learned can still create its task a
    moment later; the next beat then reports the bot running, and the gate
    reads the newest answer, so that stale "idle" never survives to a
    retirement that is itself a drain deadline away.
    """

    async def probe() -> LaneQuietAnswer | None:
        idle_before = not bots_running()
        account = await observe_account()
        if account is None:
            return None
        return LaneQuietAnswer(
            observed_at_ms=account.observed_at_ms,
            runner_idle=idle_before and not bots_running(),
            broker_work_ended=account.broker_work_ended,
            account_flat=account.account_flat,
            intents_resolved=account.intents_resolved,
        )

    return probe


@dataclass
class FleetLaneBoot:
    """One lane's live fleet state for the process lifetime."""

    presence: FleetPresence
    clerk_id: str
    worker_key: str
    volume_root: Path
    registry_id: str
    volume_id: str
    broker: str = "alpaca"
    session: SessionInfo | None = None
    #: The deployment-approved endpoint reference this lane registered with,
    #: re-presented on every re-registration. The coordinator keeps a
    #: session's current reference only while its row survives; a session it
    #: lost is re-created citing whatever the registration named, and a
    #: replacement citing none is refused delivery while the lane goes on
    #: heartbeating as reachable. Offline boots register nothing and keep
    #: ``None``.
    endpoint_ref: str | None = None
    offline_reason: str | None = None
    owned_service: FleetControlService | None = field(default=None, repr=False)
    #: What every beat reports, re-read on each pass. The default is the
    #: unbound lane — present, not bound, and honest about knowing neither
    #: its endpoint mode nor its authority. ``confirm_and_report`` replaces
    #: it if and when a binding installs.
    reported_facts: Mapping[str, object] = field(
        default_factory=lambda: heartbeat_facts(
            account_pin=None,
            effective_binding_generation=0,
            authority_kind="unavailable",
            endpoint_mode="unidentified",
        )
    )
    #: The grant this lane last confirmed, kept for the same reason
    #: ``endpoint_ref`` is: the beat may have to present it again under a
    #: replacement session. ``None`` until a confirmation succeeds — an
    #: unbound lane has nothing to re-present, and neither has a lane whose
    #: confirmation was refused.
    confirmed_grant: ConfirmedGrant | None = None
    #: The session ``confirmed_grant`` was actually confirmed under. A
    #: re-registration replaces ``session`` without touching this field, so a
    #: mismatch between the two is exactly "this grant has not been
    #: re-presented to the current session yet" — the signal every beat
    #: checks (``_reconfirm_grant_if_stale``), not only the one that follows
    #: a refused observation. Written together with ``confirmed_grant``
    #: everywhere either changes; ``None`` alongside ``confirmed_grant is
    #: None`` means "nothing confirmed".
    confirmed_grant_session: SessionInfo | None = None
    #: Whether this lane has learned it is drained (#2155): set once, never
    #: cleared, by ``_learn_drain`` — from a heartbeat's lifecycle answer or
    #: the coordinator's typed registration refusal — and read by the
    #: bot-start gate and the re-confirmation retry so a drained lane serves
    #: nothing new and re-presents no grant while it winds down.
    draining: bool = False
    #: How this lane answers lane quiet once draining (#2154). Installed by
    #: the composition root once both the bot runner and the account's clerk
    #: exist; ``None`` on a lane with no clerk, which then never confirms and
    #: exits through ``force-retire``.
    lane_quiet_probe: LaneQuietProbe | None = field(default=None, repr=False)
    #: The outstanding conditions of the last answer the coordinator accepted,
    #: so the log records a change rather than repeating every beat.
    lane_quiet_outstanding: tuple[str, ...] | None = None
    heartbeat: asyncio.Task | None = field(default=None, repr=False)

    @property
    def online(self) -> bool:
        """Whether this boot carries a registered session."""
        return self.session is not None


async def open_fleet_lane(
    *, settings: FleetSettings, volume_root: Path
) -> FleetLaneBoot | None:
    """Open the lane's presence, or return ``None`` for a legacy deployment.

    ``combined`` without fleet configuration, or with a volume that was never
    enrolled, is the legacy posture: no presence, byte-for-byte today's
    behavior. A ``clerk_agent`` role is never legacy — a missing marker or
    fleet configuration there is a refusal, never a silent fallback to
    unfenced authority (ADR 0062 addendum).

    The legacy posture stays permitted for ``combined``, but it is not silent
    when the volume is *enrolled*: that combination means a deployment lost its
    fleet configuration rather than never having had it, so it is logged at
    warning level before returning.
    """
    enrolled = volume_module.marker_path(volume_root).exists()
    if settings.ROLE == "combined":
        if settings.CONTROL_DIR is None and settings.COORDINATOR_URL is None:
            if enrolled:
                # An enrolled volume with no fleet configuration is not a fresh
                # install choosing the legacy posture — it is a deployment that
                # *had* fleet configuration and lost it. The isolation this
                # volume was enrolled for is no longer in effect, and the boot
                # is otherwise indistinguishable from a normal one, so say so
                # loudly rather than reverting in silence.
                logger.warning(
                    "This clerk volume is fleet-enrolled, but no fleet "
                    "configuration is present: the process is running the "
                    "legacy UNFENCED posture over a volume that was fenced. "
                    "The deployment's fleet configuration is missing.",
                    extra={
                        "action": "fleet_configuration_missing_on_enrolled_volume",
                        "volume_root": str(volume_root),
                        "fleet_role": settings.ROLE,
                        "next_step": (
                            "Restore FLEET_ROLE and FLEET_CONTROL_DIR or "
                            "FLEET_COORDINATOR_URL for this deployment, or "
                            "retire the volume's fleet enrolment deliberately."
                        ),
                    },
                )
            return None
        if not enrolled:
            logger.info(
                "Fleet control configured but the clerk volume is not enrolled; "
                "running the legacy unfenced posture for this boot."
            )
            return None
    if settings.ROLE == "clerk_agent" and not enrolled:
        raise FleetBootRefused(
            f"The clerk volume at {volume_root} carries no fleet identity marker; "
            "a clerk agent never falls back to unfenced authority.",
            next_step="Run the enrolment ceremony on this volume before starting "
            "the agent.",
        )
    if not settings.CLERK_ID or not settings.WORKER_KEY:
        raise FleetBootRefused(
            "A fleet-enrolled lane requires FLEET_CLERK_ID and FLEET_WORKER_KEY.",
            next_step="Provide the identities the enrolment ceremony issued.",
        )

    presence: FleetPresence
    owned_service: FleetControlService | None = None
    if settings.COORDINATOR_URL is not None:
        if not settings.AGENT_SERVICE_TOKEN:
            raise FleetBootRefused(
                "A remote fleet presence requires FLEET_AGENT_SERVICE_TOKEN.",
            )
        try:
            presence = RemotePresence(
                base_url=settings.COORDINATOR_URL,
                agent_service_token=settings.AGENT_SERVICE_TOKEN,
            )
        except FleetTransportRefused as exc:
            raise FleetBootRefused(
                f"The fleet coordinator destination is refused: {exc}",
                next_step="Serve the coordinator on the private deployment "
                "network or over https.",
            ) from exc
    else:
        if settings.CONTROL_DIR is None:
            # An enrolled lane must name its coordinator one way or the
            # other; `assert` would vanish under `python -O`.
            raise FleetBootRefused(
                "An enrolled fleet lane requires FLEET_COORDINATOR_URL (agent) "
                "or FLEET_CONTROL_DIR (combined) — neither is set.",
                next_step="Set the coordinator destination for this role.",
            )
        from app.broker.fleet_composition import production_provider_adapters

        owned_service = FleetControlService(
            store=FleetRegistryStore.open(control_dir=settings.CONTROL_DIR),
            provider_adapters=production_provider_adapters(),
        )
        presence = LocalPresence(owned_service, volume_root=volume_root)

    boot = FleetLaneBoot(
        presence=presence,
        clerk_id=settings.CLERK_ID,
        worker_key=settings.WORKER_KEY,
        volume_root=volume_root,
        registry_id="",
        volume_id="",
        owned_service=owned_service,
    )
    # The writable-root fence (fleet A2 inventory): the lane's bot-binding
    # and live-bars roots must live inside the verified clerk volume — the
    # re-homing is enforced before any database writer or broker client
    # opens, not left as a deployment convention. The broker capture
    # directory is deliberately exempt: it is lane evidence on the agent's
    # own container filesystem, never a shared root.
    try:
        _fence_writable_roots(volume_root=volume_root)
    except FleetBootRefused:
        # A refusal here still leaves `owned_service`'s SQLite handle open
        # (`boot` was constructed above solely to carry it) — close it before
        # propagating, same as every other pre-registration refusal below.
        await close_fleet_lane(boot)
        raise
    try:
        expectation = await presence.expectation(clerk_id=settings.CLERK_ID)
        boot.registry_id = str(expectation["registry_id"])
        boot.volume_id = str(expectation["volume_id"])
        _verify_root_against_expectation(volume_root, expectation, clerk_id=settings.CLERK_ID)
        if isinstance(presence, LocalPresence):
            # `register` re-proves the volume itself, so this is redundant —
            # deliberately: it is an earlier-failure belt. A wrong volume
            # fails here, before any registry transaction is opened.
            await presence.verify_volume(clerk_id=settings.CLERK_ID, volume_root=volume_root)
        boot.session = await presence.register(
            clerk_id=settings.CLERK_ID,
            worker_key=settings.WORKER_KEY,
            agent_instance_id=new_agent_instance_id(),
            endpoint_ref=settings.AGENT_ENDPOINT_REF,
            adapter_version=_ADAPTER.adapter_version,
            fleet_protocol_version=FLEET_PROTOCOL_VERSION,
        )
        # Kept on the boot so a re-registration presents the same reference:
        # it is the lane's for its whole lifetime, not this one call's.
        boot.endpoint_ref = settings.AGENT_ENDPOINT_REF
    except FleetLaneDraining as exc:
        # The lesson, not a transport failure (#2155): mark the evidence on
        # this volume so no later offline boot can present the drained
        # binding, then refuse — a drained lane never re-enrols.
        _learn_drain(boot)
        await close_fleet_lane(boot)
        raise FleetBootRefused(
            f"The fleet coordinator refused this lane's registration because "
            f"it is drained: {exc.message}",
            next_step="Finish the drain ceremony on the coordinator; this "
            "volume's evidence is marked drained and boots nothing.",
        ) from exc
    except FleetPresenceError as exc:
        # Offline rule (FR-066): the evidence must name THIS volume's
        # enrolled identity — a foreign or hand-copied evidence file does
        # not vouch for anything here.
        marker = volume_module.read_volume_marker(volume_root)
        evidence = read_confirmation_evidence(volume_root)
        if marker is not None and evidence is not None and (
            evidence.clerk_id != marker.clerk_id or evidence.volume_id != marker.volume_id
        ):
            await close_fleet_lane(boot)
            raise FleetBootRefused(
                "The confirmation evidence on this volume names clerk "
                f"{evidence.clerk_id}/volume {evidence.volume_id}; the volume's "
                f"marker names {marker.clerk_id}/{marker.volume_id}. Evidence "
                "never migrates onto another lane.",
                next_step="Restore this volume's own evidence from backup or "
                "re-enrol it with the coordinator available.",
            ) from exc
        if evidence is None:
            await close_fleet_lane(boot)
            raise FleetBootRefused(
                f"The fleet coordinator is unreachable and this volume carries no "
                f"confirmation evidence: {exc.message}",
                next_step="A first enrolment or a never-confirmed binding waits "
                "for the coordinator; it never boots unfenced.",
            ) from exc
        if evidence.lifecycle_state != StoredLifecycleState.PROVISIONED.value:
            # The resurrection block (#2155): a coordinator outage is exactly
            # when a drained lane must NOT fall back to its last-effective
            # binding. The lane itself marked this file when it learned the
            # drain; the mark, not the coordinator's availability, decides.
            await close_fleet_lane(boot)
            raise FleetBootRefused(
                "The confirmation evidence on this volume is marked drained; "
                "a drained lane boots nothing, with or without the "
                "coordinator.",
                next_step="Finish the drain ceremony on the coordinator and "
                "decommission this volume; a drained lane never returns to "
                "service.",
            ) from exc
        boot.session = None
        boot.offline_reason = exc.message
        logger.warning(
            "Fleet coordinator unreachable; booting offline against confirmed "
            "evidence (routing stays closed until the coordinator returns).",
            extra={"clerk_id": boot.clerk_id},
        )
    return boot


async def reserve_account(boot: FleetLaneBoot, *, external_account_id: str) -> None:
    """Reserve the broker-qualified account before custody opens (FR-063)."""
    if boot.session is None:
        raise FleetBootRefused(
            "A first account reservation requires the coordinator; offline boot "
            "reserves nothing.",
        )
    await boot.presence.reserve(
        broker=boot.broker, clerk_id=boot.clerk_id, external_account_id=external_account_id
    )


async def confirm_binding(
    boot: FleetLaneBoot,
    *,
    external_account_id: str,
    binding_generation: int,
    effective_profile_id: str | None,
    effective_revision: int | None,
    assignment_observed: Mapping[str, object] | None = None,
) -> AccountAssignmentRecord:
    """Confirm the binding observation and persist the clerk's evidence.

    The session is read once, before the await: a beat refused while the
    coordinator is confirming re-registers the lane and replaces
    ``boot.session`` underneath, so a second read would write evidence naming
    a session that confirmed nothing — and the next boot's FR-066 recovery
    reads that evidence as fact.

    Recording what was confirmed is already this function's job, so the
    in-process record the beat re-presents (``boot.confirmed_grant``) — and
    the session it was confirmed under (``boot.confirmed_grant_session``) —
    is written here beside the durable evidence, and by the same rule: only
    after the coordinator has accepted it. A drained refusal is the mirror
    of that rule: the volume's evidence is *un*-written into its tombstone
    before the refusal re-raises (#2155).
    """
    session = boot.session
    if session is None:
        raise FleetBootRefused(
            "A binding confirmation requires the coordinator; an offline boot "
            "confirms nothing and stays unrouted.",
        )
    try:
        confirmed = await boot.presence.confirm(
            broker=boot.broker,
            clerk_id=boot.clerk_id,
            external_account_id=external_account_id,
            binding_generation=binding_generation,
            agent_instance_id=session.agent_instance_id,
            routing_epoch=session.routing_epoch,
            effective_profile_id=effective_profile_id,
            effective_revision=effective_revision,
        )
    except FleetLaneDraining as exc:
        # The confirmation can be the lane's FIRST news of its drain — the
        # coordinator closed the door between this lane's reserve and its
        # confirm — and this is the last point the lesson can arrive before
        # the evidence write below: mark the volume, refuse the binding, and
        # let the caller surface it (#2155). Without this handler the refusal
        # escapes unlearned and the provisioned evidence stays FR-066-usable.
        _learn_drain(boot)
        raise FleetBootRefused(
            f"The fleet coordinator refused this lane's binding confirmation "
            f"because it is drained: {exc.message}",
            next_step="Finish the drain ceremony on the coordinator; this "
            "volume's evidence is marked drained and confirms nothing.",
        ) from exc
    write_confirmation_evidence(
        boot.volume_root,
        ConfirmationEvidence(
            clerk_id=boot.clerk_id,
            volume_id=boot.volume_id,
            registry_id=boot.registry_id,
            assignment_generation=confirmed.assignment_generation,
            canonical_account_id=confirmed.canonical_external_account_id,
            binding_generation=binding_generation,
            effective_profile_id=effective_profile_id,
            effective_revision=effective_revision,
            confirmed_at_ms=now_ms_utc(),
            agent_instance_id=session.agent_instance_id,
            routing_epoch=session.routing_epoch,
        ),
    )
    boot.confirmed_grant = ConfirmedGrant(
        external_account_id=external_account_id,
        binding_generation=binding_generation,
        effective_profile_id=effective_profile_id,
        effective_revision=effective_revision,
    )
    boot.confirmed_grant_session = session
    del assignment_observed  # reserved for the delivery-B forwarding facts
    return confirmed


def offline_boot_matches(
    boot: FleetLaneBoot,
    *,
    canonical_account_id: str,
    effective_profile_id: str | None,
    effective_revision: int | None,
    binding_generation: int | None = None,
) -> bool:
    """FR-066: only the evidence-confirmed exact grant may boot offline.

    Drained evidence never matches, whatever the tuple (#2155): the lane
    marked its own file when it learned the drain, and the mark survives the
    coordinator outage that follows.
    """
    evidence = read_confirmation_evidence(boot.volume_root)
    if evidence is not None and boot.registry_id == "":
        boot.registry_id = evidence.registry_id
        boot.volume_id = evidence.volume_id
    return evidence_vouches_for(
        evidence,
        canonical_account_id=canonical_account_id,
        effective_profile_id=effective_profile_id,
        effective_revision=effective_revision,
        binding_generation=binding_generation,
    )


def _live_account_nickname(account_id: str | None) -> str | None:
    """The account's nickname, read fresh from the profiles store.

    Deliberately not cached anywhere on ``FleetLaneBoot``: called from every
    beat (``_beat``, below) so a rename via ``PUT /account-nicknames/
    {account_id}`` reaches this lane's heartbeat on the next beat rather than
    waiting for this process to restart. The import stays local to this
    function — the established pattern at this module boundary (see
    ``main.py``'s own local import of the same accessor) rather than a
    module-level one.
    """
    if account_id is None:
        return None
    from app.broker_configuration.runtime import get_broker_configuration_service

    return get_broker_configuration_service().nickname_for(account_id)


async def _guarded_live_nickname(*, clerk_id: str, account_id: str | None) -> str | None:
    """``_live_account_nickname``, made safe for the heartbeat's fatal path.

    A cosmetic display name must never be able to end the beat or block the
    event loop, so this wraps the raw read with the two guards it needs:

    - **Off the loop.** ``_live_account_nickname`` goes through
      ``ProfilesStore._query_one``, which takes the same ``RLock`` a write
      ``transaction()`` (``BEGIN IMMEDIATE`` included) holds for its whole
      duration. Every other async caller of the profiles service already
      wraps it in ``asyncio.to_thread`` (see
      ``routers/broker_configuration.py``); this is that same pattern applied
      here.
    - **Never fatal.** ``get_broker_configuration_service()`` can raise
      ``ProfilesDatabaseUnavailable`` on first use (filesystem check,
      cross-process advisory lock, schema migration), and the query itself
      can raise a raw ``sqlite3.OperationalError`` — neither may reach
      ``_beat``'s own broad ``except Exception`` (that one ends the lane's
      presence until the process restarts). A failed read is logged and
      treated as "no nickname to report this beat", the same outcome as one
      simply not being set.

    A nickname longer than the coordinator's own bound
    (``records.py``'s ``_ACCOUNT_NICKNAME_MAX_CHARS``) is dropped the same
    way, rather than trusting that constant to still agree with whatever
    wrote this value — a mismatch there would otherwise refuse every beat
    from this lane as an identity mismatch, re-registering (and climbing the
    routing epoch) on every single one.
    """
    if account_id is None:
        return None
    try:
        nickname = await asyncio.to_thread(_live_account_nickname, account_id)
    except Exception:
        logger.warning(
            "Account nickname read failed; omitting from this beat",
            extra={"clerk_id": clerk_id, "action": "nickname_read_failed"},
        )
        return None
    if nickname is not None and len(nickname) > _ACCOUNT_NICKNAME_MAX_CHARS:
        logger.warning(
            "Account nickname exceeds the coordinator's bound; omitting from this beat",
            extra={"clerk_id": clerk_id, "action": "nickname_too_long"},
        )
        return None
    return nickname


def _summary_with_live_nickname(
    summary: object, nickname: str | None
) -> Mapping[str, object] | None:
    """``summary`` with its ``account_nickname`` replaced by a fresh read.

    Everything else in the bounded summary (``endpoint_mode``,
    ``authority_state``, ``detail``) is confirm-time state that only changes
    on a rebind, so only this one key gets touched here — the rest is
    whatever ``heartbeat_facts`` last computed at confirm/boot.
    """
    if not isinstance(summary, Mapping):
        return None
    refreshed = dict(summary)
    if nickname is not None:
        refreshed["account_nickname"] = nickname
    else:
        refreshed.pop("account_nickname", None)
    return refreshed


def _learn_drain(boot: FleetLaneBoot) -> None:
    """Record, exactly once, that this lane learned it is drained (#2155).

    The two ways a lane learns are both routed here: a heartbeat whose answer
    carries ``lifecycle_state: draining``, and the coordinator's typed
    ``FleetLaneDraining`` refusal at registration. The durable act is
    re-authoring the volume's confirmation evidence into its tombstone, so
    the very outage that hides the coordinator cannot conjure the binding
    back through FR-066's offline boot; the in-process acts — stopping new
    bot starts and grant re-presentations — read ``boot.draining``.
    """
    if boot.draining:
        return
    # The latch flips only after the durable mark succeeds: an I/O failure on
    # the evidence write must leave this call retryable, or the surviving
    # provisioned evidence would vouch for a later offline boot forever.
    marked = mark_confirmation_evidence_draining(boot.volume_root)
    boot.draining = True
    logger.warning(
        "This lane learned it is drained; its confirmation evidence is "
        "marked and new bot starts refuse while the drain completes.",
        extra={
            "clerk_id": boot.clerk_id,
            "action": "fleet_lane_drain_learned",
            "evidence_marked": marked,
        },
    )
    if boot.lane_quiet_probe is None:
        # Said once, here, because the gate's own refusal only reaches the
        # operator running retire: a lane with no clerk to read its account
        # never confirms lane quiet, so this drain can only end forced.
        logger.warning(
            "This drained lane cannot answer lane quiet; it retires only "
            "through force-retire, after its account is closed at the broker.",
            extra={"clerk_id": boot.clerk_id, "action": "lane_quiet_unanswerable"},
        )


def start_heartbeat(boot: FleetLaneBoot, *, interval_s: float) -> asyncio.Task:
    """Observe on a cadence; a refused beat repairs the lane.

    Called when the lane opens, so an unbound lane is present too. Each beat
    re-reads ``boot.reported_facts`` rather than closing over them: a binding
    that installs later changes what the lane says without restarting the
    task that says it. The task is stored on ``boot.heartbeat`` so
    ``close_fleet_lane`` ends the beat with the lane.

    The confirmed account's nickname gets the same treatment, one level
    deeper: ``boot.reported_facts`` itself only changes when ``confirm_and_
    report`` runs (a rebind), but a Configuration rename between rebinds must
    still reach the wire (PRD #2182 — "one account name everywhere" is not
    true if the badge and card can go stale for the lane's whole remaining
    lifetime). So every beat re-reads the nickname fresh from the profiles
    store, off the loop and guarded against ever ending the beat
    (``_guarded_live_nickname``), and folds it into the summary this
    particular send carries, without mutating the stored snapshot — the same
    "no restart required" property, applied one field further down.

    An observation itself never confirms anything. The refusal path is the
    exception, and it is repair rather than protocol: a beat that re-registers
    replaces the session its binding was confirmed under, so it re-presents
    that grant immediately (``_repair_lane_after_refused_beat``). If that
    immediate re-presentation is itself refused (a single transient error
    right after re-registration), the grant stays pending rather than
    dropped, and every later beat — refused or not — retries it
    (``_reconfirm_grant_if_stale``) instead of waiting for a second refused
    observation that may never come.
    """

    async def _beat() -> None:
        try:
            while True:
                await asyncio.sleep(interval_s)
                reported = boot.reported_facts
                summary = reported.get("reported_summary")
                if boot.session is None:
                    continue
                account_id = reported.get("reported_account_id")
                live_summary = _summary_with_live_nickname(
                    summary,
                    await _guarded_live_nickname(
                        clerk_id=boot.clerk_id,
                        account_id=account_id if isinstance(account_id, str) else None,
                    ),
                )
                try:
                    learned_lifecycle = await boot.presence.observe(
                        clerk_id=boot.clerk_id,
                        agent_instance_id=boot.session.agent_instance_id,
                        reported_binding_generation=reported.get("reported_binding_generation"),
                        reported_account_id=reported.get("reported_account_id"),
                        reported_state=reported.get("reported_state"),
                        reported_summary=live_summary,
                    )
                except FleetLaneDraining:
                    # The typed refusal is news about this lane, not a broken
                    # beat: learn it and keep beating — no repair is possible
                    # for a lane the coordinator will not re-register.
                    _learn_drain(boot)
                except FleetControlError as exc:
                    logger.warning(
                        "Fleet heartbeat refused: %s", exc.message, extra={"clerk_id": boot.clerk_id}
                    )
                    await _repair_lane_after_refused_beat(boot)
                else:
                    if learned_lifecycle == StoredLifecycleState.DRAINING.value:
                        _learn_drain(boot)
                    # The common case is a no-op: `confirmed_grant_session`
                    # already names this session. It only does work when a
                    # prior re-registration's immediate re-confirmation
                    # attempt (above) was itself refused and left the grant
                    # pending — this is that retry, running on every landed
                    # beat rather than waiting for another refusal.
                    await _reconfirm_grant_if_stale(boot)
                await _confirm_lane_quiet_if_draining(boot)
        except asyncio.CancelledError:
            # Normal shutdown (stop_heartbeat's task.cancel()) is not a death
            # to log — re-raise it untouched.
            raise
        except Exception:
            # Anything other than a FleetControlError anywhere in the loop is
            # unexpected (a malformed coordinator response, a raw sqlite3
            # error from the local store, a transport or filesystem error on
            # the repair path above) and ends the beat — but silently, unless
            # announced right here: the task simply stops, and nothing notices
            # until the coordinator eventually projects the lane
            # `unreachable`, or `stop_heartbeat` logs the already-dead task at
            # shutdown. `_repair_lane_after_refused_beat` absorbs the
            # coordinator's own refusals and nothing else, so every other exit
            # path — including one raised from inside the `except` clause
            # above, which Python never routes to a sibling `except` of the
            # same `try` — shares this one log site.
            logger.exception(
                "Fleet heartbeat ended on an unexpected exception; lane "
                "presence has stopped until this process restarts.",
                extra={"clerk_id": boot.clerk_id},
            )
            raise

    boot.heartbeat = asyncio.create_task(
        _beat(), name=f"fleet-heartbeat-{boot.clerk_id}"
    )
    return boot.heartbeat


async def _confirm_lane_quiet_if_draining(boot: FleetLaneBoot) -> None:
    """Send a fresh lane-quiet answer while this lane is draining (#2154).

    Every beat, not once: the coordinator's gate reads the answer only while
    it is younger than its validity window, so a lane that stopped confirming
    refuses exactly as a dead one does. Nothing here may end the beat — a
    lane whose presence stopped would be projected unreachable while it still
    holds the account — so a refused confirmation and a failed observation
    are both logged and retried on the next beat.
    """
    session = boot.session
    if not boot.draining or boot.lane_quiet_probe is None or session is None:
        return
    try:
        answer = await asyncio.wait_for(
            boot.lane_quiet_probe(), timeout=LANE_QUIET_OBSERVATION_TIMEOUT_S
        )
    except TimeoutError:
        logger.warning(
            "Lane-quiet observation timed out; no confirmation this beat",
            extra={"clerk_id": boot.clerk_id, "action": "lane_quiet_observation_timed_out"},
        )
        return
    except Exception:
        logger.exception(
            "Lane-quiet observation failed; no confirmation this beat",
            extra={"clerk_id": boot.clerk_id, "action": "lane_quiet_observation_failed"},
        )
        return
    if answer is None:
        return
    try:
        await boot.presence.confirm_lane_quiet(
            clerk_id=boot.clerk_id,
            agent_instance_id=session.agent_instance_id,
            routing_epoch=session.routing_epoch,
            observed_at_ms=answer.observed_at_ms,
            runner_idle=answer.runner_idle,
            broker_work_ended=answer.broker_work_ended,
            account_flat=answer.account_flat,
            intents_resolved=answer.intents_resolved,
        )
    except FleetControlError as exc:
        logger.warning(
            "Lane-quiet confirmation refused: %s",
            exc.message,
            extra={"clerk_id": boot.clerk_id, "action": "lane_quiet_confirmation_refused"},
        )
        return
    if answer.outstanding != boot.lane_quiet_outstanding:
        logger.info(
            "Lane-quiet answer changed",
            extra={
                "clerk_id": boot.clerk_id,
                "action": "lane_quiet_confirmed",
                "quiet": not answer.outstanding,
                "outstanding": list(answer.outstanding),
            },
        )
    boot.lane_quiet_outstanding = answer.outstanding


async def stop_heartbeat(boot: FleetLaneBoot | None) -> None:
    """End the lane's beat, idempotently.

    Called twice by design: the service teardown stops the beat *first* — the
    bots, consumers, custody handles and repositories come down after it, and
    a lane that kept beating through that stretch would be telling the
    coordinator a dismantling clerk was reachable — and ``close_fleet_lane``
    calls it again on its way out. A boot with no beat (``None``, offline, or
    already stopped) is a no-op, not a refusal.

    ``_beat`` logs a ``FleetControlError`` around its observation and retries
    (re-registering the session); anything else (a malformed coordinator
    response, a raw ``sqlite3`` error from the local store) is logged at the
    point of death — never silently — and still ends the task, with that
    exception stored on it. ``cancel()`` is a no-op on a task that's already
    done, so awaiting it would re-raise that stored exception here — the
    first statement of the service teardown's ``finally`` block — aborting
    every step after it
    (``bot_task_registry.stop_all()``, the consumer stop, the custody and
    repository closes). The beat's death must not mask the clerk's teardown,
    so a beat found already done is logged and swallowed instead of awaited.
    """
    if boot is None or boot.heartbeat is None:
        return
    beat = boot.heartbeat
    boot.heartbeat = None
    if not beat.done():
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
        return
    if not beat.cancelled():
        exc = beat.exception()
        if exc is not None:
            logger.warning(
                "Fleet heartbeat had already ended with an error before shutdown: %s",
                exc,
                extra={"clerk_id": boot.clerk_id},
                exc_info=exc,
            )


def binding_is_granted(
    *, account_pin: str | None, effective_binding_generation: int
) -> bool:
    """Whether this boot has a binding worth confirming to the coordinator.

    Generation 0 is "no grant has been applied yet", not "generation zero" —
    a lane whose selection never reached ``selection/apply`` carries a pin
    the operator typed and nothing the registry ever granted. The one place
    this question is answered: callers ask here rather than restating the
    two halves and drifting apart.
    """
    return bool(account_pin) and effective_binding_generation >= 1


def heartbeat_facts(
    *,
    account_pin: str | None,
    effective_binding_generation: int,
    authority_kind: str,
    endpoint_mode: str,
) -> Mapping[str, object]:
    """What this lane reports on every beat, given its binding state.

    ``endpoint_mode`` is ``paper``/``live`` once a binding installs (the
    settings' own mode) and ``unidentified`` before one does — a lane with no
    binding has no endpoint to name. ``sqlite`` with ``unidentified`` cannot
    occur: a sqlite authority implies an installed binding, and that path
    always passes the settings mode.

    Both shapes carry the same bounded typed summary
    (``ProviderSummaryObservation``): endpoint mode and authority state, and
    nothing else. That is not a style choice — a summary the coordinator
    cannot parse is refused as an identity mismatch, and ``start_heartbeat``
    answers a refusal by registering a fresh session, so one free-form key
    here would climb the routing epoch on every beat instead of failing once
    and visibly. The account's nickname (PRD #2182) is not part of this
    confirm-time snapshot — it is folded in fresh, per beat, by
    ``start_heartbeat`` (see its docstring), which is the only place that
    ever writes ``account_nickname`` onto the outgoing summary.

    An unbound lane reports ``binding_pending`` with no generation: it is
    saying "I am here, I am not bound", which projects ``starting`` rather
    than ``unreachable``, and keeps the configuration surface reachable. It
    still reports its pin and authority state — that is how the desk shows
    *why* the lane is unbound rather than merely that it is.
    """
    authority_state = {
        "sqlite": f"real_{endpoint_mode}",
        "shadow": "shadow",
        "synthetic": "synthetic",
        "unavailable": "unavailable",
    }.get(authority_kind, "unavailable")
    summary = {"endpoint_mode": endpoint_mode, "authority_state": authority_state}
    if binding_is_granted(
        account_pin=account_pin, effective_binding_generation=effective_binding_generation
    ):
        return {
            "reported_binding_generation": effective_binding_generation,
            "reported_account_id": account_pin,
            "reported_state": "binding_confirmed",
            "reported_summary": summary,
        }
    return {
        "reported_binding_generation": None,
        "reported_account_id": account_pin,
        "reported_state": "binding_pending",
        "reported_summary": summary,
    }


async def confirm_and_report(
    boot: FleetLaneBoot,
    *,
    account_pin: str | None,
    effective_binding_generation: int,
    effective_profile_id: str | None,
    effective_revision: int | None,
    authority_kind: str,
    endpoint_mode: str,
) -> None:
    """Confirm the grant if there is one; report what installed either way.

    The confirmation is conditional — only a granted binding is worth
    confirming to the coordinator. What the lane *reports* is updated
    unconditionally: an installation that produced no grant still changed what
    this lane is, and the desk reads that from the beat.

    No task is created here. The beat has been running since the lane opened
    and re-reads ``boot.reported_facts`` on every pass, so the binding lands
    under it rather than replacing it.
    """
    if binding_is_granted(
        account_pin=account_pin, effective_binding_generation=effective_binding_generation
    ):
        # `binding_is_granted` has already established the pin is present.
        await confirm_binding(
            boot,
            external_account_id=account_pin,
            binding_generation=effective_binding_generation,
            effective_profile_id=effective_profile_id,
            effective_revision=effective_revision,
        )
    else:
        # Stated rather than assumed: whatever this lane confirmed before is
        # not what installed now, so the beat has nothing to re-present. A
        # grant left standing here would be re-confirmed under a replacement
        # session for a binding that is no longer the effective one.
        boot.confirmed_grant = None
        boot.confirmed_grant_session = None
    boot.reported_facts = heartbeat_facts(
        account_pin=account_pin,
        effective_binding_generation=effective_binding_generation,
        authority_kind=authority_kind,
        endpoint_mode=endpoint_mode,
    )


async def close_fleet_lane(boot: FleetLaneBoot | None) -> None:
    """Release the boot's heartbeat, transport and any owned service."""
    if boot is None:
        return
    # The beat observes through the very presence this closes next, so it
    # ends first: the lane's lifetime is the beat's at both ends. The service
    # teardown has usually stopped it already; stopping it is idempotent.
    await stop_heartbeat(boot)
    try:
        await boot.presence.close()
    except Exception as exc:
        logger.warning(
            "Fleet presence close failed during lane shutdown: %s", exc,
            extra={"clerk_id": boot.clerk_id},
        )
    if boot.owned_service is not None:
        boot.owned_service.close()


async def _repair_lane_after_refused_beat(boot: FleetLaneBoot) -> None:
    """Re-register the lost session, then re-present the grant it had confirmed.

    A coordinator that restarted lost this session: a fresh registration
    returns the lane to routed without a process restart. It cites the
    reference this lane registered with, which the re-registration always
    should have carried — a coordinator that lost the session re-creates it
    citing whatever arrives here, and a session naming no approved endpoint is
    refused every delivery while the lane keeps beating as reachable.

    The replacement session has confirmed nothing, and the coordinator fences
    a confirmed observation by the instance id and routing epoch that wrote it
    (``service._descriptor``) — so a lane that re-registered under a live
    binding projects ``starting`` rather than ``ready`` until it confirms
    again. Leaving that to "the next boot" (``confirmation.py``'s recovery
    story, written for a lost confirmation *reply*, not for a session this
    process replaced) makes a human restarting the container the only repair.
    The gap opens on an asymmetric failure: the observation is refused — a
    transient 5xx is enough — and the registration answering it succeeds.

    Both the registration and the re-confirmation are repairs of a
    coordinator that is already misbehaving, and a beat that died on either
    would trade a lane the desk shows as ``starting`` for one the coordinator
    eventually projects ``unreachable`` — so both are logged and dropped
    rather than raised. They are not symmetric past that: the registration is
    only retried by the *next refused* beat (this function runs nowhere
    else), while the re-confirmation it fires immediately below is retried by
    *every* later beat, refused or not (``_reconfirm_grant_if_stale``,
    called again from ``_beat``'s normal path) — because a lane with a live,
    valid session has no reason to wait for another refusal before trying
    again to present a grant that session has not yet confirmed. Only a
    ``FleetControlError`` is absorbed here; anything else (a raw transport or
    filesystem error) still ends the beat at ``_beat``'s one log site,
    deliberately.
    """
    try:
        boot.session = await boot.presence.register(
            clerk_id=boot.clerk_id,
            worker_key=boot.worker_key,
            agent_instance_id=new_agent_instance_id(),
            endpoint_ref=boot.endpoint_ref,
            adapter_version=_ADAPTER.adapter_version,
            fleet_protocol_version=FLEET_PROTOCOL_VERSION,
        )
    except FleetLaneDraining:
        # Not repairable by re-registration: the refusal is the drain lesson
        # itself. Mark the evidence, leave the beat to keep observing what
        # it can, and stop trying to rebuild a session.
        _learn_drain(boot)
        return
    except FleetControlError as exc:
        logger.warning(
            "Fleet re-registration refused: %s",
            exc.message,
            extra={"clerk_id": boot.clerk_id},
        )
        return
    logger.info(
        "Fleet session re-registered after heartbeat refusal.",
        extra={"clerk_id": boot.clerk_id},
    )
    await _reconfirm_grant_if_stale(boot)


async def _reconfirm_grant_if_stale(boot: FleetLaneBoot) -> None:
    """Re-present the confirmed grant if the current session hasn't confirmed it.

    ``boot.confirmed_grant_session`` names the session that actually
    confirmed ``boot.confirmed_grant``; a re-registration replaces
    ``boot.session`` without touching either field, so the two fall out of
    step the moment the session changes and stay out of step until a
    confirmation under the new session succeeds. A lane with no confirmed
    grant has nothing to re-present (``binding_is_granted`` already gates
    what gets confirmed in the first place), and an offline lane has no
    session to present it under.

    Called from two places: immediately after ``_repair_lane_after_refused_beat``
    re-registers, and from every other landed beat in ``_beat``. The first
    call is what used to be this function's entire job; the second is the
    fix — it is what lets a re-confirmation that is itself refused (a single
    transient error right after re-registration) retry on a later, ordinary
    beat instead of being logged, dropped, and never tried again until
    another observation happens to be refused too.
    """
    grant = boot.confirmed_grant
    session = boot.session
    if grant is None or session is None or boot.confirmed_grant_session == session:
        return
    if boot.draining:
        # A drained lane re-presents no grant (#2155): the coordinator's
        # confirmation gate refuses it anyway, and a retry loop against a
        # refusal that is news — not breakage — is noise.
        return
    try:
        await confirm_binding(
            boot,
            external_account_id=grant.external_account_id,
            binding_generation=grant.binding_generation,
            effective_profile_id=grant.effective_profile_id,
            effective_revision=grant.effective_revision,
        )
    except FleetControlError as exc:
        logger.warning(
            "Fleet binding re-confirmation refused; a later beat retries it: %s",
            exc.message,
            extra={"clerk_id": boot.clerk_id},
        )
        return
    logger.info(
        "Fleet binding re-confirmed under the current session.",
        extra={"clerk_id": boot.clerk_id},
    )


def _fence_writable_roots(*, volume_root: Path) -> None:
    """Refuse writable lane roots that escape the verified clerk volume.

    The bot-binding root (``IBKR_LIVE_RUNS_ROOT``'s parent — the live_state
    tree and arming seals) and the live-bars aggregation root must resolve
    inside the clerk volume for every enrolled lane — ``combined``-with-a-
    marker included, since two enrolled combined processes on one host share
    the same shared artifacts tree exactly as two agents would; a deployment
    that left them on the shared artifacts tree would let two lanes write one
    root, which is exactly what the fleet exists to prevent (fleet A2
    inventory; audit 2026-09-13, finding 5).

    The bot-binding root must moreover **be** the volume root, not merely
    sit inside it (#2269): export's "every bot stopped" check reads the
    copied volume's bot state at its root, exactly where the lane-wide
    stop's intent sweep must have written it, so a deeper state root would
    hide every bot from that check.
    """
    from app.broker.ibkr.config import get_settings as get_ibkr_settings

    volume = volume_root.resolve()
    ibkr_settings = get_ibkr_settings()
    fenced = {
        "live_state_root": Path(ibkr_settings.live_runs_root).parent,
        "live_bars_root": Path(ibkr_settings.live_bars_root),
    }
    for label, root in fenced.items():
        resolved = root.resolve()
        if resolved != volume and volume not in resolved.parents:
            raise FleetBootRefused(
                f"The {label} at {resolved} escapes the clerk volume {volume}; "
                "a clerk agent's writable roots are lane-local by fence, not "
                "convention.",
                next_step="Re-home the root inside the clerk volume "
                "(e.g. <clerk_dir>/live_runs and <clerk_dir>/live_bars) and "
                "restart the agent.",
            )
    state_root = fenced["live_state_root"].resolve()
    if state_root != volume:
        raise FleetBootRefused(
            f"The live_state_root at {state_root} is not the clerk volume root {volume}; "
            "a lane's bot state lives at its volume root, where the lane-wide stop and "
            "installation export read it.",
            next_step="Set IBKR_LIVE_RUNS_ROOT to <clerk_dir>/live_runs and restart the "
            "agent.",
        )


def _verify_root_against_expectation(
    volume_root: Path, expectation: Mapping[str, object], *, clerk_id: str
) -> None:
    """Prove the mounted root against the registry-served identity."""
    volume_module.verify_volume_identity(
        volume_root,
        expected_broker=str(expectation["broker"]),
        expected_clerk_id=clerk_id,
        expected_volume_id=str(expectation["volume_id"]),
        expected_attestation_kind=str(expectation["attestation_kind"]),
        expected_attestation_id=str(expectation["attestation_id"]),
    )


__all__ = [
    "ConfirmedGrant",
    "FleetBootRefused",
    "FleetLaneBoot",
    "LaneQuietAnswer",
    "LaneQuietProbe",
    "binding_is_granted",
    "close_fleet_lane",
    "confirm_and_report",
    "confirm_binding",
    "confirmation_evidence_path",
    "heartbeat_facts",
    "lane_quiet_probe",
    "offline_boot_matches",
    "open_fleet_lane",
    "reserve_account",
    "start_heartbeat",
    "stop_heartbeat",
]
