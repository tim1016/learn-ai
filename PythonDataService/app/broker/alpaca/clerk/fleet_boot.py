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
4. ``start_heartbeat`` — observations only; they never confirm anything.

With the coordinator unreachable mid-boot, the offline rule is FR-066: only
a recovered binding that exactly matches the confirmation evidence boots
(last-effective recovery); a first assignment or a changed binding waits for
the coordinator, failing closed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from app.broker.alpaca.clerk.fleet_adapter import AlpacaProviderAdapter
from app.broker.fleet import volume as volume_module
from app.broker.fleet.confirmation import (
    ConfirmationEvidence,
    confirmation_evidence_path,
    evidence_vouches_for,
    read_confirmation_evidence,
    write_confirmation_evidence,
)
from app.broker.fleet.errors import FleetControlError
from app.broker.fleet.identity import new_agent_instance_id
from app.broker.fleet.internal_http import FleetTransportRefused
from app.broker.fleet.presence import (
    FleetPresence,
    FleetPresenceError,
    LocalPresence,
    RemotePresence,
    SessionInfo,
)
from app.broker.fleet.provider import FLEET_PROTOCOL_VERSION
from app.broker.fleet.records import AccountAssignmentRecord
from app.broker.fleet.service import FleetControlService, FleetRegistryStore
from app.config import FleetSettings
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

_ADAPTER = AlpacaProviderAdapter()


class FleetBootRefused(FleetControlError):
    """The lane may not open authority under the fleet's admission rules.

    Internal family: boot fails closed and the refusal names the ceremony
    that resolves it.
    """

    reason = "fleet_boot_refused"
    status_code = 409


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
    offline_reason: str | None = None
    owned_service: FleetControlService | None = field(default=None, repr=False)

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
    """Confirm the binding observation and persist the clerk's evidence."""
    if boot.session is None:
        raise FleetBootRefused(
            "A binding confirmation requires the coordinator; an offline boot "
            "confirms nothing and stays unrouted.",
        )
    confirmed = await boot.presence.confirm(
        broker=boot.broker,
        clerk_id=boot.clerk_id,
        external_account_id=external_account_id,
        binding_generation=binding_generation,
        agent_instance_id=boot.session.agent_instance_id,
        routing_epoch=boot.session.routing_epoch,
        effective_profile_id=effective_profile_id,
        effective_revision=effective_revision,
    )
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
            agent_instance_id=boot.session.agent_instance_id,
            routing_epoch=boot.session.routing_epoch,
        ),
    )
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
    """FR-066: only the evidence-confirmed exact grant may boot offline."""
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


def start_heartbeat(
    boot: FleetLaneBoot,
    *,
    interval_s: float,
    facts: Callable[[], Mapping[str, object]],
) -> asyncio.Task:
    """Observe on a cadence; observations never confirm anything."""

    async def _beat() -> None:
        from app.broker.fleet.identity import new_agent_instance_id

        while True:
            await asyncio.sleep(interval_s)
            reported = facts()
            summary = reported.get("reported_summary")
            if boot.session is None:
                continue
            try:
                await boot.presence.observe(
                    clerk_id=boot.clerk_id,
                    agent_instance_id=boot.session.agent_instance_id,
                    reported_binding_generation=reported.get("reported_binding_generation"),
                    reported_account_id=reported.get("reported_account_id"),
                    reported_state=reported.get("reported_state"),
                    reported_summary=dict(summary) if isinstance(summary, Mapping) else None,
                )
            except FleetControlError as exc:
                logger.warning(
                    "Fleet heartbeat refused: %s", exc.message, extra={"clerk_id": boot.clerk_id}
                )
                # A coordinator that restarted lost this session: present a
                # fresh registration so the lane returns to routed without a
                # process restart. The confirmation on the assignment is not
                # touched — a re-confirmation is the next boot's first act.
                try:
                    boot.session = await boot.presence.register(
                        clerk_id=boot.clerk_id,
                        worker_key=boot.worker_key,
                        agent_instance_id=new_agent_instance_id(),
                        endpoint_ref=None,
                        adapter_version=_ADAPTER.adapter_version,
                        fleet_protocol_version=FLEET_PROTOCOL_VERSION,
                    )
                    logger.info(
                        "Fleet session re-registered after heartbeat refusal.",
                        extra={"clerk_id": boot.clerk_id},
                    )
                except FleetControlError as register_exc:
                    logger.warning(
                        "Fleet re-registration refused: %s",
                        register_exc.message,
                        extra={"clerk_id": boot.clerk_id},
                    )

    return asyncio.create_task(_beat(), name=f"fleet-heartbeat-{boot.clerk_id}")


async def close_fleet_lane(boot: FleetLaneBoot | None) -> None:
    """Release the boot's transport and any owned service."""
    if boot is None:
        return
    try:
        await boot.presence.close()
    except Exception as exc:
        logger.warning(
            "Fleet presence close failed during lane shutdown: %s", exc,
            extra={"clerk_id": boot.clerk_id},
        )
    if boot.owned_service is not None:
        boot.owned_service.close()


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
    "FleetBootRefused",
    "FleetLaneBoot",
    "close_fleet_lane",
    "confirm_binding",
    "confirmation_evidence_path",
    "offline_boot_matches",
    "open_fleet_lane",
    "reserve_account",
    "start_heartbeat",
]
