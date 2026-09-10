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
    AlpacaCredentialEnvironment,
    AlpacaRuntimeContext,
    BrokerProfileError,
    resolve_runtime_context,
)
from app.broker_configuration.binding_decision import (
    BindingCandidate,
    BindingIntent,
    NothingToBind,
    decide,
    needs_acknowledgement,
    switch_verdict,
)
from app.broker_configuration.errors import BrokerConfigurationError
from app.broker_configuration.records import ProfileRevision
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

    if chosen.intent is BindingIntent.APPLY:
        return await _bind_applied(
            chosen, service=service, probe=probe, environment=environment
        )
    return _bind_candidate(chosen, service=service, environment=environment)


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

    _record_refusal(service, candidate=candidate, reason=outcome)
    return _boot_last_effective_after_refusal(
        service=service, candidate=candidate, reason=outcome, environment=environment
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
    previous_account_id = candidate.previous_account_id
    # The second clause is implied by the first — a verdict can only require a
    # clear prior account when there *is* one — and is written out rather than
    # asserted, because an assertion is removed under ``python -O`` and this is
    # the guard that keeps a live position from being stranded.
    if verdict.requires_prior_account_clear() and previous_account_id is not None:
        prior = await probe.observe(previous_account_id)
        if not prior.is_clear:
            return (
                f"applying {candidate.profile_id}@{candidate.revision} would leave "
                f"{prior.describe()}"
            )

    try:
        return _resolve(stored, candidate, environment=environment)
    except BrokerProfileError as exc:
        return f"the staged revision could not be resolved: {exc.reason}"


def _bind_candidate(
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
    return BoundWorker(context=context, candidate=candidate)


def _boot_last_effective_after_refusal(
    *,
    service: BrokerConfigurationService,
    candidate: BindingCandidate,
    reason: str,
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
        # Re-read: recording the refusal advanced the generation.
        selection_generation=service.selection().selection_generation,
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
    return _bind_candidate(fallback, service=service, environment=environment)


def _record_refusal(
    service: BrokerConfigurationService, *, candidate: BindingCandidate, reason: str
) -> None:
    """Consume the one-shot Apply so an unattended restart cannot re-arm it.

    A failure to *record* the refusal must not stop the worker booting its
    last-effective revision — the refusal already happened, and the worst case
    is that the Apply is retried and refused again next start.
    """
    try:
        service.record_apply_refusal(
            reason=reason, expected_selection_generation=candidate.selection_generation
        )
    except BrokerConfigurationError as exc:
        logger.warning(
            "Could not record the Apply refusal; the worker still boots last-effective",
            extra={"action": "worker_binding_refusal_unrecorded", "reason": exc.reason},
        )


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


def account_pin_disagreement(bound: BoundWorker, *, account_id: str | None) -> str | None:
    """Why custody's account contradicts the revision's pin, or ``None``.

    Contract §3: "The pin is re-observed at apply and at startup", and an
    observation that contradicts a pin refuses. The re-observation at startup is
    not a second broker call — the Clerk already resolved the account from the
    revision's own credentials — so this compares what custody actually opened
    on against what the operator explicitly approved.

    The case it catches is narrow and nasty: a credential slot whose injected
    pair has been repointed at a *different* Alpaca account. Everything else
    still looks right — the profile is applied, the mode agrees, the envelope is
    intact — and the worker would take custody of, and trade, an account nobody
    approved.

    A revision with no pin has nothing to contradict, and neither does the
    pre-cutover environment bootstrap, which binds no revision at all.
    """
    context = bound.context
    if context.account_pin is None or account_id is None:
        return None
    if context.account_pin == account_id:
        return None
    return (
        f"this revision is pinned to account {context.account_pin} but its credentials "
        f"opened custody on account {account_id}"
    )


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
    "account_pin_disagreement",
    "acknowledge_worker_binding",
    "resolve_worker_binding",
]
