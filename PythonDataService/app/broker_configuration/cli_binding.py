"""Resolving the effective broker revision from an operator CLI.

The three ``manage_alpaca_*`` ceremonies are not the worker. They are separate
processes where ``app/main.py``'s lifespan has installed no binding, so each has
to resolve one itself — and they must resolve the *same* revision the worker
would, or an operator could arm one configuration while the service runs
another.

They deliberately resolve **less** than
:func:`~app.broker_configuration.worker_binding.resolve_worker_binding`: they
never consume the one-shot Apply, never record an apply refusal, and never
acknowledge an effective binding. Applying a staged revision is the worker's
job, and a read-only ``status`` that advanced ``selection_generation`` would
silently conflict whatever generation a browser is holding.

This lives beside the worker's own resolution rather than inside a CLI because
it is domain logic about the profiles database, not CLI transport — and because
the alternative had ``manage_alpaca_sqlite_clerk.py``, an offline broker-free
recovery tool, importing the arming ceremony to borrow it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from app.broker.alpaca.active_binding import (
    BROKER_UNCONFIGURED,
    PROFILES_DATABASE_UNAVAILABLE,
    BrokerUnbound,
    UnboundBroker,
)
from app.broker.alpaca.config import AlpacaSettings, get_alpaca_settings
from app.broker.alpaca.profile import BrokerProfileError, resolve_runtime_context
from app.broker_configuration import runtime as broker_configuration_runtime
from app.broker_configuration.binding_decision import NothingToBind, decide
from app.broker_configuration.errors import (
    BrokerConfigurationError,
    ProfilesDatabaseUnavailable,
)
from app.broker_configuration.records import InstallationSelection
from app.broker_configuration.selection import reference as revision_reference
from app.broker_configuration.service import BrokerConfigurationService
from app.broker_configuration.store import profiles_database_path


@dataclass(frozen=True)
class EffectiveBroker:
    """The settings an operator CLI runs against, and the selection behind them.

    ``selection`` is ``None`` in the two cases where no profiles database chose
    the settings: a caller that injected an ``AlpacaSettings`` directly (a test,
    or a future embedder), and an installation with no profiles database at all.
    Neither can have a staged revision, which is exactly what a ``None``
    selection means to the staged-versus-effective check.
    """

    settings: AlpacaSettings
    selection: InstallationSelection | None


ProfilesServiceFactory = Callable[[], BrokerConfigurationService | None]


def _installed_profiles_service() -> BrokerConfigurationService | None:
    """This installation's configuration service, or ``None`` before cutover.

    The existence check is what keeps these commands read-only.
    ``ProfilesStore.open`` *creates and migrates* the database, and a ``status``
    that conjured an empty profiles database onto the Clerk volume would be
    writing on a read path. A database that is not there is precisely the state
    ``binding_decision`` names ``installation_is_unconfigured`` -- this
    installation has never saved a profile -- so answering ``None`` here and
    letting the environment bootstrap answer is the same decision the worker
    makes, taken without the write.

    Both names are looked up on the ``runtime`` module rather than imported
    from it, and that is load-bearing: ``tests/conftest.py`` keeps every test's
    profiles database inside ``tmp_path`` by patching
    ``runtime.resolve_clerk_dir``, and a ``from``-import here would hold the
    original function and reach a developer's real Clerk volume instead.
    """
    if not profiles_database_path(broker_configuration_runtime.resolve_clerk_dir()).exists():
        return None
    return broker_configuration_runtime.get_broker_configuration_service()


def _unbound_from(exc: BrokerConfigurationError) -> UnboundBroker:
    """A configuration refusal, in ``active_binding``'s two-code vocabulary."""
    if isinstance(exc, ProfilesDatabaseUnavailable):
        return UnboundBroker(
            reason=PROFILES_DATABASE_UNAVAILABLE,
            message=exc.message,
            next_step=exc.next_step or "Check the Clerk volume is mounted and readable, then retry.",
        )
    return UnboundBroker(
        reason=BROKER_UNCONFIGURED,
        message=exc.message,
        next_step=exc.next_step or "Repair the saved broker configuration, then retry.",
    )


def effective_broker(
    *, service_factory: ProfilesServiceFactory = _installed_profiles_service
) -> EffectiveBroker:
    """The **effective** profile revision's settings, resolved in this process.

    The three ``manage_alpaca_*`` CLIs are not the worker: they are separate
    processes where ``app/main.py``'s lifespan has installed no binding, so each
    has to resolve one itself. They deliberately resolve *less* than
    ``broker_configuration.worker_binding.resolve_worker_binding``: they never
    consume the one-shot Apply, never record an apply refusal and never
    acknowledge an effective binding. Applying a staged revision is the worker's
    job, and a read-only ``status`` that advanced ``selection_generation`` would
    silently conflict whatever generation a browser is holding.

    The revision chosen is therefore exactly the one a restart with no Apply
    pending would bind: ``decide`` is asked the very same question with
    ``apply_requested`` cleared, so this resolver and the worker's cannot drift
    into two different answers about what "effective" means.

    Raises :class:`BrokerUnbound` when the installation is configured but has
    applied nothing, when its profiles database is unreadable, or when the
    effective revision will not resolve -- there is no fallback to stale
    environment settings on a configured installation (ADR 0060 Decision 7).
    Lets ``pydantic.ValidationError`` out of the pre-cutover environment
    bootstrap, which every caller already translates into its own vocabulary.
    """
    try:
        service = service_factory()
        if service is None:
            return EffectiveBroker(settings=get_alpaca_settings(), selection=None)
        selection = service.selection()
        has_any_profile = bool(service.list_profiles(include_archived=True))
    except BrokerConfigurationError as exc:
        raise BrokerUnbound(_unbound_from(exc)) from exc

    chosen = decide(replace(selection, apply_requested=False), has_any_profile=has_any_profile)
    if isinstance(chosen, NothingToBind):
        if not chosen.installation_is_unconfigured:
            raise BrokerUnbound(
                UnboundBroker(
                    reason=BROKER_UNCONFIGURED,
                    message=(
                        "No saved broker configuration has been applied, so this command has "
                        "no effective revision to run against."
                    ),
                    next_step=(
                        "Stage a profile revision and press Apply, then restart the service "
                        "so the worker binds it."
                    ),
                )
            )
        return EffectiveBroker(settings=get_alpaca_settings(), selection=selection)

    try:
        stored = service.read_revision(chosen.profile_id, chosen.revision)
    except BrokerConfigurationError as exc:
        raise BrokerUnbound(_unbound_from(exc)) from exc
    try:
        context = resolve_runtime_context(
            endpoint_mode=stored.endpoint_mode,
            credential_slot=stored.credential_slot,
            live_envelope=(
                None if stored.live_envelope is None else stored.live_envelope.to_mapping()
            ),
            account_pin=stored.account_pin,
            profile_id=chosen.profile_id,
            revision=chosen.revision,
        )
    except BrokerProfileError as exc:
        raise BrokerUnbound(
            UnboundBroker(
                reason=BROKER_UNCONFIGURED,
                message=(
                    f"The effective broker revision {revision_reference(chosen.profile_id, chosen.revision)} "
                    f"could not be loaded: {exc.message}"
                ),
                next_step="Check the credential slot it names is still injected, then retry.",
            )
        ) from exc
    return EffectiveBroker(settings=context.settings, selection=selection)


def effective_alpaca_settings(
    *, service_factory: ProfilesServiceFactory = _installed_profiles_service
) -> AlpacaSettings:
    """:func:`effective_broker`'s settings, for the callers with no staged check.

    The shadow and SQLite-Clerk CLIs read one scalar apiece off the effective
    revision and have no arming ceremony to gate, so they take this shape rather
    than restating the resolution.
    """
    return effective_broker(service_factory=service_factory).settings


__all__ = [
    "EffectiveBroker",
    "ProfilesServiceFactory",
    "effective_alpaca_settings",
    "effective_broker",
]
