"""Resolving the one broker binding this worker runs on, at startup.

This is the ceremony ADR 0060 Decision 5 describes, in the order the contract
requires it: choose a revision, refuse a switch that would strand a prior
account, resolve the credentials, and — only after construction succeeded and
the worker owns the account's execution lease — write the effective binding
back to the installation's selection row.

The lease is not acquired here, and deliberately not re-implemented here: it is
a row in the account's own SQLite under ``clerk_dir``, taken by
``repository_lifecycle._acquire_execution_lease`` when the Clerk authority
opens. So "the worker owns the lease" is exactly "a Clerk authority opened for
the bound account", which is why this module has two entry points rather than
one: :func:`resolve_worker_binding` runs *before* the broker and the Clerk are
built, and :func:`acknowledge_worker_binding` runs *after*, with the account the
Clerk actually bound.

**The refusal paths are the point of the module.** Owner decision 4: a refused
Apply, or a crash with a staged selection, boots the last-*effective* revision
and never leaves a worker with no broker while a position is open. Every
refusal here therefore falls back to the last-effective revision rather than
returning empty-handed, and the one case that legitimately has nothing to fall
back to — an installation whose very first Apply is refused — never had a
binding to strand.

**Nothing here runs per tick.** A running worker resolves its context once; EXIT
and reconciliation never read the profiles database again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.broker.alpaca.active_binding import (
    ACCOUNT_PIN_MISMATCH,
    APPLY_PREFLIGHT_REFUSED,
    BROKER_UNCONFIGURED,
    PROFILES_DATABASE_UNAVAILABLE,
    UnboundBroker,
)
from app.broker.alpaca.profile import (
    AccountPinMismatch,
    AlpacaCredentialEnvironment,
    AlpacaRuntimeContext,
    BrokerProfileError,
    resolve_runtime_context,
    reverify_pinned_account,
    verify_account,
)
from app.broker_configuration import legacy_environment
from app.broker_configuration.binding_decision import (
    BindingCandidate,
    BindingIntent,
    NothingToBind,
    decide,
    needs_acknowledgement,
    switch_verdict,
)
from app.broker_configuration.errors import BrokerConfigurationError
from app.broker_configuration.legacy_environment import (
    retired_environment_refusal,
    stale_retired_settings,
)
from app.broker_configuration.records import InstallationSelection, ProfileRevision
from app.broker_configuration.runtime import get_broker_configuration_service
from app.broker_configuration.service import BrokerConfigurationService

ServiceFactory = Callable[[], BrokerConfigurationService]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriorObligations:
    """What a previously-bound account still owes, or that we cannot tell.

    ``readable`` is not a diagnostic; it is half the decision. The plan's rule
    is that a start which cannot *prove* the prior account is clear must refuse,
    so an unreadable probe and a probe reporting an open position lead to the
    same place.
    """

    account_id: str
    blocking_facts: tuple[str, ...] = ()
    readable: bool = True

    @property
    def is_clear(self) -> bool:
        return self.readable and not self.blocking_facts

    def describe(self) -> str:
        if not self.readable:
            return (
                f"account {self.account_id} could not be inspected, so this start "
                "cannot prove it has no open obligations"
            )
        return f"account {self.account_id} still has " + ", ".join(self.blocking_facts)


@runtime_checkable
class PriorAccountObligations(Protocol):
    """Whether a previously-bound account is provably clear of obligations."""

    async def observe(self, account_id: str) -> PriorObligations: ...


class UnprovableObligations:
    """The fail-closed default: no probe installed, so nothing can be proven clear.

    A build with no probe can still *recover* its own account — recovery never
    asks this question — but it can never switch away from one, which is the
    correct direction to fail.
    """

    async def observe(self, account_id: str) -> PriorObligations:
        return PriorObligations(account_id=account_id, readable=False)


@dataclass(frozen=True)
class BoundWorker:
    """A resolved binding, ready to be installed and built from."""

    context: AlpacaRuntimeContext
    candidate: BindingCandidate | None
    """``None`` on the pre-cutover environment bootstrap, which binds no revision."""

    @property
    def from_profile(self) -> bool:
        return self.candidate is not None


@dataclass(frozen=True)
class UnboundWorker:
    """No binding could be installed, and the operator-facing reason."""

    unbound: UnboundBroker


ResolvedWorkerBinding = BoundWorker | UnboundWorker


def _unconfigured(message: str, next_step: str) -> UnboundWorker:
    return UnboundWorker(
        UnboundBroker(reason=BROKER_UNCONFIGURED, message=message, next_step=next_step)
    )


async def resolve_worker_binding(
    *,
    service_factory: ServiceFactory = get_broker_configuration_service,
    obligations: PriorAccountObligations | None = None,
    environment: AlpacaCredentialEnvironment | None = None,
) -> ResolvedWorkerBinding:
    """Choose, preflight and resolve the revision this start binds.

    The service arrives as a factory so that *opening* the profiles database is
    inside the same refusal path as reading it: an unreadable database is one
    contract-named outcome (``profiles_database_unavailable``) whichever step
    discovers it.
    """
    probe = obligations or UnprovableObligations()
    try:
        service = service_factory()
        selection = await asyncio.to_thread(service.selection)
        has_any_profile = bool(await asyncio.to_thread(service.list_profiles, include_archived=True))
    except BrokerConfigurationError as exc:
        # An unreadable profiles database closes the gate and surfaces why. It
        # must not crash-loop the service (#2014) and must not silently revert
        # to the environment: this installation *is* configured, we simply
        # cannot read which way.
        logger.warning(
            "Broker profiles database unreadable; no broker binding installed",
            extra={"action": "worker_binding_database_unavailable", "reason": exc.reason},
        )
        return UnboundWorker(
            UnboundBroker(
                reason=PROFILES_DATABASE_UNAVAILABLE,
                message="The saved broker configuration could not be read, so no broker is bound.",
                next_step="Check the Clerk volume is mounted and readable, then restart the service.",
            )
        )

    chosen = decide(selection, has_any_profile=has_any_profile)
    if isinstance(chosen, NothingToBind):
        return _bind_nothing(chosen, environment=environment)

    # This installation has cut over: a saved revision is about to be bound, so
    # the variables it replaced are stale by definition. The owner's resolution
    # of ADR 0060 open question 1 (2026-09-10) is to answer them rather than
    # ignore them quietly, which ``extra="ignore"`` would otherwise do — and,
    # as narrowed the same day, to answer them *differently* depending on why
    # this worker is binding at all.
    #
    # **Apply refuses; recovery complains and binds.** A deliberate Apply is a
    # request to change what is in force, and a stale line means two documents
    # claim to describe the change: refusing is the only honest answer, and an
    # operator is right there to delete the line. An ordinary restart — a crash,
    # a reboot, an OOM kill, ``restart: always`` — requests no change at all.
    # Refusing *it* would collide with owner decision 4 (ADR 0060 D4.3), the
    # rule this whole module is shaped around: a boot must never leave the
    # worker with no broker while a position could be open, because nothing can
    # EXIT it. One leftover harmless line must not strand the account on every
    # restart from now until an operator happens to read the log.
    #
    # So the recovery path logs the same fact, at the same level, every boot,
    # naming the same variables — the hygiene problem stays loud and visible —
    # and then binds. Nothing it binds comes from these variables: the effective
    # revision supplies the mode and the envelope, and ``resolved_alpaca_settings``
    # answers every downstream consumer from the installed binding, so a stale
    # value is never mistaken for the one in force.
    #
    # The Apply refusal is deliberately *this* refusal and not a raise: the gate
    # closes with a named reason and the service still boots (#2014), so an
    # operator who left a line behind fixes it by deleting the line, not by
    # debugging a crash loop. The check sits above every broker and Clerk
    # construction, so nothing takes an execution lease or reaches the network
    # first.
    #
    # Order matters. It is *below* ``_bind_nothing`` because a pre-cutover
    # installation legitimately runs on these variables, and *inside* the intent
    # branch because the intent is exactly the distinction the owner drew.
    #
    # One read, off the loop thread. ``LegacyEnvironmentPresence()`` parses
    # ``.env`` from disk; reading it twice — once for the refusal, once for the
    # log line — would both block here and let the two disagree.
    #
    # Reached through the module rather than imported by name, deliberately.
    # ``tests/conftest.py``'s autouse fixture drops the ``.env`` half of this
    # reader so no test inherits a developer's real file; a ``from ... import``
    # binds the function object here at import time and that patch would never
    # reach this call — which is exactly how these tests came to pass in a fresh
    # worktree and fail in the main checkout.
    presence = await asyncio.to_thread(legacy_environment.current_retired_settings)
    stale_refusal = retired_environment_refusal(presence)

    if chosen.intent is BindingIntent.APPLY:
        if stale_refusal is not None:
            logger.error(
                "Retired broker settings are still present; the Apply is refused and "
                "no broker binding installed",
                extra={
                    "action": "worker_binding_retired_environment",
                    "reason_code": stale_refusal.reason,
                    "retired_variables": list(stale_retired_settings(presence)),
                },
            )
            # An Apply must be *consumed* even when it is refused (ADR 0060 D4.3):
            # leaving the one-shot request pending would let a later restart — after
            # someone tidies ``.env`` for an unrelated reason — silently apply the
            # very change this boot refused. ``_record_refusal`` is the same path
            # every other apply refusal takes.
            _record_refusal(service, candidate=chosen, reason=stale_refusal.reason)
            return UnboundWorker(stale_refusal)
        return await _bind_applied(
            chosen, service=service, probe=probe, environment=environment
        )

    if stale_refusal is not None:
        # A recovery consumes nothing: no Apply is pending on this path, so
        # there is no one-shot request to record a refusal against, and calling
        # ``_record_refusal`` here would write a refusal for a change nobody
        # asked for. The level stays ``error`` — this is operator-actionable and
        # repeats every boot until the line is deleted — but the ``action``
        # differs so a log search or an alert rule can tell "refused to bind"
        # from "bound anyway, please tidy up".
        #
        # The wording claims only what is certain at this point. The bind below
        # can still be refused for an unrelated reason — an unresolvable
        # revision, an account-pin mismatch — and a line asserting "bound
        # anyway" would be the one an operator read on exactly that boot.
        logger.error(
            "Retired broker settings are still present; this recovery does not read "
            "them and is not refused for them",
            extra={
                "action": "worker_binding_retired_environment_recovery_warning",
                "reason_code": stale_refusal.reason,
                "retired_variables": list(stale_retired_settings(presence)),
            },
        )
    return await _bind_candidate(chosen, service=service, environment=environment)


def _bind_nothing(
    chosen: NothingToBind, *, environment: AlpacaCredentialEnvironment | None
) -> ResolvedWorkerBinding:
    """Nothing is effective. Either bootstrap, or close the gate."""
    if not chosen.installation_is_unconfigured:
        logger.warning(
            "No effective broker profile; the broker gate is closed",
            extra={"action": "worker_binding_unconfigured"},
        )
        return _unconfigured(
            "No saved broker configuration has been applied, so no broker is bound.",
            "Stage a profile revision and press Apply, then restart the service.",
        )

    # Pre-cutover: this installation has never saved a profile, so there is no
    # user-owned configuration to prefer and the environment is still the only
    # description of the worker. Package F's import retires this branch by
    # writing the first profile; from that moment ``has_any_profile`` is true
    # and this code is unreachable. It is deliberately *not* a failure
    # fallback — a configured installation that fails to bind lands above, with
    # the gate closed.
    return _bootstrap_from_environment(environment=environment)


def _bootstrap_from_environment(
    *, environment: AlpacaCredentialEnvironment | None
) -> ResolvedWorkerBinding:
    from pydantic import ValidationError

    from app.broker.alpaca.config import (
        alpaca_configuration_error_detail,
        get_alpaca_settings,
    )
    from app.broker.alpaca.profile.credentials import resolve_credentials

    try:
        settings = get_alpaca_settings()
    except ValidationError as exc:
        # The detail names the missing ``ALPACA_LIVE_*`` variables, and on this
        # path that is the true diagnostic: no profile exists, so the
        # environment really is the source, and an operator with a half-edited
        # ``.env`` needs to know which line is missing. It goes to the log
        # only. The operator-facing ``message`` below stays in profile
        # vocabulary, because contract §6 renders it verbatim in the UI and
        # ADR 0060 supersedes exactly the environment-source rule that prose
        # states. ``alpaca_configuration_error_detail`` keeps only Pydantic's
        # ``msg`` text, which never echoes the credential-bearing input.
        logger.warning(
            "No saved broker configuration and no usable environment settings; "
            "the broker gate is closed",
            extra={
                "action": "worker_binding_bootstrap_unavailable",
                "detail": alpaca_configuration_error_detail(exc),
            },
        )
        return _unconfigured(
            "No saved broker configuration exists and the environment does not "
            "describe a usable one, so no broker is bound.",
            "Create a broker profile, verify its account, then stage and apply it.",
        )

    try:
        credentials = resolve_credentials("default", environment=environment)
    except BrokerProfileError:
        return _unconfigured(
            "No saved broker configuration exists and no credentials are injected, "
            "so no broker is bound.",
            "Inject a credential pair, then create and apply a broker profile.",
        )

    logger.warning(
        "No broker profile exists; bootstrapping this worker from the process "
        "environment. Import the configuration into a profile to retire this path.",
        extra={"action": "worker_binding_environment_bootstrap", "mode": settings.mode},
    )
    from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues

    return BoundWorker(
        context=AlpacaRuntimeContext(
            settings=settings,
            credentials=credentials,
            live_envelope=None if settings.is_paper else LiveEnvelopeValues.from_settings(settings),
        ),
        candidate=None,
    )


async def _bind_applied(
    candidate: BindingCandidate,
    *,
    service: BrokerConfigurationService,
    probe: PriorAccountObligations,
    environment: AlpacaCredentialEnvironment | None,
) -> ResolvedWorkerBinding:
    """Bind a staged revision an Apply named, or refuse and boot last-effective."""
    outcome = await _attempt_apply(
        candidate, service=service, probe=probe, environment=environment
    )
    if not isinstance(outcome, str):
        return BoundWorker(context=outcome, candidate=candidate)

    recorded = _record_refusal(service, candidate=candidate, reason=outcome)
    return await _boot_last_effective_after_refusal(
        service=service,
        candidate=candidate,
        reason=outcome,
        recorded=recorded,
        environment=environment,
    )


async def _attempt_apply(
    candidate: BindingCandidate,
    *,
    service: BrokerConfigurationService,
    probe: PriorAccountObligations,
    environment: AlpacaCredentialEnvironment | None,
) -> AlpacaRuntimeContext | str:
    """The context an Apply would bind, or the reason it is refused.

    Preflight and resolution are one step because on this path they have one
    outcome: a staged revision that cannot be resolved must be *refused* —
    recording the refusal and consuming the one-shot Apply — and not merely
    fail, or every subsequent restart retries the same broken Apply.
    """
    try:
        stored = _read_revision(service, candidate)
    except BrokerConfigurationError as exc:
        return f"the staged revision could not be read: {exc.message}"

    verdict = switch_verdict(candidate, candidate_account_pin=stored.account_pin)
    if verdict.requires_prior_account_clear():
        previous_account_id = candidate.previous_account_id
        if previous_account_id is None:
            # A revision was effective but its account was never recorded, so
            # there is nothing to hand the probe. Refusing is the whole point of
            # the verdict — skipping to the resolve below would be the
            # fail-open this branch exists to close.
            return (
                f"applying {candidate.profile_id}@{candidate.revision} would replace a "
                "binding whose account was never recorded, so this start cannot prove "
                "the previous account has no open obligations"
            )
        prior = await probe.observe(previous_account_id)
        if not prior.is_clear:
            return (
                f"applying {candidate.profile_id}@{candidate.revision} would leave "
                f"{prior.describe()}"
            )

    try:
        context = _resolve(stored, candidate, environment=environment)
    except BrokerProfileError as exc:
        return f"the staged revision could not be resolved: {exc.reason}"

    mismatch = await _pin_reobservation_refusal(context)
    if mismatch is not None:
        return f"the staged revision is not approved for the account it reaches: {mismatch}"
    return context


async def _bind_candidate(
    candidate: BindingCandidate,
    *,
    service: BrokerConfigurationService,
    environment: AlpacaCredentialEnvironment | None,
) -> ResolvedWorkerBinding:
    """Resolve one candidate into a context, or close the gate."""
    try:
        stored = _read_revision(service, candidate)
        context = _resolve(stored, candidate, environment=environment)
    except (BrokerConfigurationError, BrokerProfileError) as exc:
        logger.warning(
            "The effective broker revision could not be resolved; the gate is closed",
            extra={
                "action": "worker_binding_revision_unresolvable",
                "profile_id": candidate.profile_id,
                "revision": candidate.revision,
                "reason": getattr(exc, "reason", type(exc).__name__),
            },
        )
        return _unconfigured(
            "The broker configuration this installation last applied could not be "
            "loaded, so no broker is bound.",
            "Check the credential slot it names is still injected, then restart the service.",
        )

    mismatch = await _pin_reobservation_refusal(context)
    if mismatch is not None:
        logger.error(
            "The applied revision's credentials reach an account it is not approved for; "
            "no broker binding installed",
            extra={
                "action": "worker_binding_account_pin_mismatch",
                "profile_id": candidate.profile_id,
                "revision": candidate.revision,
            },
        )
        return UnboundWorker(
            UnboundBroker(
                reason=ACCOUNT_PIN_MISMATCH,
                message=(
                    "The applied broker configuration is approved for one account but "
                    "its credentials reach another, so no broker is bound."
                ),
                next_step=(
                    "Restore the credential pair for the approved account, or verify "
                    "and apply a revision approved for the account they now reach."
                ),
            )
        )
    return BoundWorker(context=context, candidate=candidate)


async def _boot_last_effective_after_refusal(
    *,
    service: BrokerConfigurationService,
    candidate: BindingCandidate,
    reason: str,
    recorded: InstallationSelection | None,
    environment: AlpacaCredentialEnvironment | None,
) -> ResolvedWorkerBinding:
    """Owner decision 4: a refused Apply still boots the last-effective revision."""
    if candidate.previous_profile_id is None or candidate.previous_revision is None:
        # The installation's very first Apply was refused. There is no prior
        # binding to fall back to and — precisely because there is none — no
        # position, order or custody this leaves stranded.
        logger.warning(
            "The first Apply was refused and there is no previous binding to boot",
            extra={"action": "worker_binding_apply_refused_cold", "reason": reason},
        )
        return UnboundWorker(
            UnboundBroker(
                reason=APPLY_PREFLIGHT_REFUSED,
                message=f"The configuration could not be applied: {reason}.",
                next_step="Resolve what is blocking it, then stage and apply again.",
            )
        )

    fallback = BindingCandidate(
        profile_id=candidate.previous_profile_id,
        revision=candidate.previous_revision,
        intent=BindingIntent.RECOVER,
        # Recording the refusal advanced the generation, and ``record_apply_refusal``
        # already returned the row it wrote. Re-reading it here instead would put
        # the module's one unguarded database call on the *refusal* path: a blip
        # there would escape into the lifespan and abort startup — a crash loop
        # (#2014) in exactly the state this branch exists for, where an Apply was
        # just refused because the prior account still holds a position and
        # nobody can EXIT it if the worker cannot boot. When the refusal was not
        # recorded, the stale generation stands and the acknowledgement fence
        # simply refuses the write later, which is harmless.
        selection_generation=(
            candidate.selection_generation if recorded is None else recorded.selection_generation
        ),
        previous_profile_id=candidate.previous_profile_id,
        previous_revision=candidate.previous_revision,
        previous_account_id=candidate.previous_account_id,
    )
    logger.warning(
        "Apply refused; booting the last-effective broker revision",
        extra={
            "action": "worker_binding_apply_refused",
            "refused_profile_id": candidate.profile_id,
            "refused_revision": candidate.revision,
            "booted_profile_id": fallback.profile_id,
            "booted_revision": fallback.revision,
            "reason": reason,
        },
    )
    return await _bind_candidate(fallback, service=service, environment=environment)


def _record_refusal(
    service: BrokerConfigurationService, *, candidate: BindingCandidate, reason: str
) -> InstallationSelection | None:
    """Consume the one-shot Apply so an unattended restart cannot re-arm it.

    Returns the row it wrote, so the caller needs no second read. A failure to
    *record* the refusal must not stop the worker booting its last-effective
    revision — the refusal already happened, and the worst case is that the
    Apply is retried and refused again next start.
    """
    try:
        return service.record_apply_refusal(
            reason=reason, expected_selection_generation=candidate.selection_generation
        )
    except BrokerConfigurationError as exc:
        logger.warning(
            "Could not record the Apply refusal; the worker still boots last-effective",
            extra={"action": "worker_binding_refusal_unrecorded", "reason": exc.reason},
        )
        return None


def _read_revision(
    service: BrokerConfigurationService, candidate: BindingCandidate
) -> ProfileRevision:
    return service.read_revision(candidate.profile_id, candidate.revision)


def _resolve(
    stored: ProfileRevision,
    candidate: BindingCandidate,
    *,
    environment: AlpacaCredentialEnvironment | None,
) -> AlpacaRuntimeContext:
    return resolve_runtime_context(
        endpoint_mode=stored.endpoint_mode,
        credential_slot=stored.credential_slot,
        live_envelope=(
            None if stored.live_envelope is None else stored.live_envelope.to_mapping()
        ),
        account_pin=stored.account_pin,
        profile_id=candidate.profile_id,
        revision=candidate.revision,
        environment=environment,
    )


async def _pin_reobservation_refusal(context: AlpacaRuntimeContext) -> str | None:
    """Re-observe the revision's pinned account, before anything takes custody.

    Contract §3: "The pin is re-observed at apply and at startup." The case it
    catches is narrow and nasty — a credential slot whose injected pair has been
    repointed at a *different* Alpaca account. Everything else still looks right:
    the profile is applied, the mode agrees, the envelope is intact. Nothing
    else in the boot would notice, and the worker would take custody of, and
    trade, an account nobody approved.

    This runs **before** the Clerk is composed, deliberately. Checking after
    would mean the authority had already opened and taken that account's
    execution lease — writing to, and locking, the very account being refused,
    which is precisely the side effect ``prior_obligations`` goes to such
    lengths to avoid.

    **Only a definite contradiction refuses.** A broker that cannot be reached
    at startup is not a mismatch, and treating it as one would turn a transient
    network blip into a worker with no broker for the life of the process —
    strictly worse than today, where the boot proceeds and authority selection
    reports ``BROKER_ACCOUNT_UNAVAILABLE`` and can recover. Every non-mismatch
    failure therefore falls through to that existing path.
    """
    if context.account_pin is None:
        return None
    try:
        verification = await verify_account(context)
        reverify_pinned_account(verification, pinned_account_id=context.account_pin)
    except AccountPinMismatch as exc:
        return exc.message
    except BrokerProfileError as exc:
        logger.warning(
            "Could not re-observe the pinned account at startup; authority "
            "selection will report what it finds",
            extra={"action": "worker_binding_pin_reobservation_unavailable", "reason": exc.reason},
        )
    return None


def acknowledge_worker_binding(
    *,
    bound: BoundWorker,
    account_id: str | None,
    service_factory: ServiceFactory = get_broker_configuration_service,
) -> None:
    """Record the effective binding, once the Clerk holds the account's lease.

    Called only after construction succeeded, so a start that failed halfway
    never publishes itself as the effective runtime. The generation fence is
    what stops a stale worker overwriting a newer binding: if anything advanced
    the selection while this worker was building, the write is refused and the
    previous effective binding stands.
    """
    candidate = bound.candidate
    if candidate is None:
        # The environment bootstrap binds no revision, so there is no effective
        # profile to acknowledge. Writing one would invent a binding the
        # operator never made.
        return
    if account_id is None:
        # No custody opened, so this worker holds no execution lease — and the
        # contract's rule is that the effective fields are written *only* once
        # it does. Writing anyway is not merely a false receipt: the
        # acknowledgement sets ``effective_account_id`` unconditionally, so a
        # boot where authority selection failed (activation missing, lease held
        # elsewhere, database refused — all states the lifespan handles and
        # logs) would NULL the remembered account. The switch preflight reads
        # exactly that field to decide whether a prior account must be proven
        # clear, so one clerk-less boot would silently disarm it and the next
        # Apply would strand an open position without ever consulting the probe.
        logger.info(
            "No Alpaca custody opened; the effective binding is left as it was",
            extra={
                "action": "worker_binding_acknowledgement_skipped_no_custody",
                "profile_id": candidate.profile_id,
                "revision": candidate.revision,
            },
        )
        return
    if not needs_acknowledgement(candidate, bound_account_id=account_id):
        return
    try:
        service_factory().acknowledge_effective(
            profile_id=candidate.profile_id,
            revision=candidate.revision,
            account_id=account_id,
            expected_selection_generation=candidate.selection_generation,
        )
    except BrokerConfigurationError as exc:
        logger.warning(
            "Could not acknowledge the effective broker binding",
            extra={
                "action": "worker_binding_acknowledgement_refused",
                "profile_id": candidate.profile_id,
                "revision": candidate.revision,
                "reason": exc.reason,
            },
        )


__all__ = [
    "ACCOUNT_PIN_MISMATCH",
    "BoundWorker",
    "PriorAccountObligations",
    "PriorObligations",
    "ResolvedWorkerBinding",
    "UnboundWorker",
    "UnprovableObligations",
    "acknowledge_worker_binding",
    "resolve_worker_binding",
]
