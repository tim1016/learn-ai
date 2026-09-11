"""The one resolved Alpaca binding this worker is running on (ADR 0060).

Every consumer that used to call ``get_alpaca_settings()`` asks here instead.
The binding is resolved **once**, at startup, by
``app/broker_configuration/worker_binding.py``, and installed here; nothing
re-reads it per tick and nothing re-reads the process environment behind it.

Three states, and the difference between them is the whole safety property:

``bound``
    A revision resolved and the worker installed it.
    :func:`resolved_alpaca_settings` answers from that revision, and the
    process environment is never consulted again on this path. This is what
    stops a stream, an account probe, or a cached snapshot from answering
    under a *different* configuration than the one the worker bound.

``refused``
    A binding was attempted and could not be installed — no effective
    selection, an unreadable profiles database, a revision that will not
    resolve. :func:`resolved_alpaca_settings` **raises**; it does not fall
    back to the environment. A configuration failure that silently reverted
    to whatever ``.env`` happens to hold is precisely the "fallback to stale
    user settings" the plan forbids (§5), and on a live installation it is how
    a worker ends up trading someone else's limits.

``unresolved``
    Nothing has attempted a binding in this process yet. Only here does
    :func:`resolved_alpaca_settings` read the process-wide settings singleton.
    Two callers are in this state legitimately: a unit test that constructs a
    consumer directly, and the pre-cutover bootstrap (see
    ``worker_binding.py`` — an installation whose profiles database holds no
    selection at all, which Package F's import retires).

The refusal vocabulary is the contract's (§6): ``broker_unconfigured`` and
``profiles_database_unavailable`` are both 503 — the gate is closed and the
reason is surfaced, and the *service still boots* (#2014). A closed gate is
never a crash loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from app.broker.alpaca.config import AlpacaSettings, get_alpaca_settings

if TYPE_CHECKING:
    # Type-only: importing the profile package at runtime would close a cycle,
    # because its account verification imports ``AlpacaBroker`` and the broker
    # imports this module for its settings. Nothing here needs the class at
    # runtime — every use is an annotation, and this module declares
    # ``from __future__ import annotations``.
    from app.broker.alpaca.profile.runtime_context import AlpacaRuntimeContext

# Contract §6 refusal codes. Code-like and stable: the Frontend renders them
# through the shared ``receiptLabel`` pipe, and the prose beside them is
# backend-authored (CLAUDE.md hard rule).
BROKER_UNCONFIGURED: Final = "broker_unconfigured"
PROFILES_DATABASE_UNAVAILABLE: Final = "profiles_database_unavailable"
APPLY_PREFLIGHT_REFUSED: Final = "apply_preflight_refused"
# Custody opened on an account the revision did not pin (contract §6). A 409
# there, but here it is a boot-time refusal: the gate closes and stays closed.
ACCOUNT_PIN_MISMATCH: Final = "account_pin_mismatch"
# A variable the profiles database replaced is still set on an installation that
# has cut over (ADR 0060 open question 1, resolved by the owner 2026-09-10:
# refuse, do not ignore quietly — narrowed the same day to *deliberate* boots
# only, so an ordinary restart logs these names and binds rather than raising
# this). Not in contract §6's table, which was written before that answer
# existed; the shape is the same and package F's ``legacy_environment`` module is
# the only raiser.
RETIRED_ENVIRONMENT_SETTINGS: Final = "retired_environment_settings"


@dataclass(frozen=True)
class UnboundBroker:
    """Why this worker has no broker binding, in the operator's words.

    ``reason`` is the contract §6 code; ``message`` and ``next_step`` are
    backend-authored prose the UI renders and never composes itself.
    """

    reason: str
    message: str
    next_step: str

    def as_detail(self) -> dict[str, str]:
        """The contract §6 error body, identical in shape to C's refusals."""
        return {"reason": self.reason, "message": self.message, "next_step": self.next_step}


class BrokerUnbound(RuntimeError):
    """Raised when a consumer needs settings and the binding was refused.

    Carries the refusal rather than a bare string so a router can translate it
    the same way it translates every other typed configuration refusal.
    """

    def __init__(self, unbound: UnboundBroker) -> None:
        super().__init__(unbound.message)
        self.unbound = unbound

    @property
    def reason(self) -> str:
        return self.unbound.reason

    @property
    def http_status(self) -> int:
        # Both refusals this raises are "the gate is closed and the reason is
        # surfaced" (contract §6), never a client mistake.
        return 503

    def as_detail(self) -> dict[str, str]:
        return self.unbound.as_detail()


_binding: AlpacaRuntimeContext | None = None
_refusal: UnboundBroker | None = None


def set_active_alpaca_binding(context: AlpacaRuntimeContext) -> None:
    """Install the binding this worker resolved. Clears any prior refusal."""
    global _binding, _refusal
    _binding = context
    _refusal = None


def refuse_active_alpaca_binding(unbound: UnboundBroker) -> None:
    """Record that no binding could be installed, and why.

    Clears any previously installed binding: a worker that has just failed to
    bind must not keep answering from the configuration it held before, or a
    refused switch would look like a successful one.
    """
    global _binding, _refusal
    _binding = None
    _refusal = unbound


def get_active_alpaca_binding() -> AlpacaRuntimeContext | None:
    """The installed binding, or ``None`` when refused or not yet resolved."""
    return _binding


def active_alpaca_binding_refusal() -> UnboundBroker | None:
    """The recorded refusal, or ``None`` when bound or not yet resolved."""
    return _refusal


def resolved_alpaca_settings() -> AlpacaSettings:
    """The settings every migrated consumer reads.

    Answers from the installed binding when there is one; raises
    :class:`BrokerUnbound` when a binding was attempted and refused; falls
    through to the process-wide singleton only when nothing has attempted a
    binding in this process at all (see the module docstring).
    """
    if _binding is not None:
        return _binding.settings
    if _refusal is not None:
        raise BrokerUnbound(_refusal)
    return get_alpaca_settings()


def reset_active_alpaca_binding_for_testing() -> None:
    """Drop both the binding and the refusal so a test can rebind."""
    global _binding, _refusal
    _binding = None
    _refusal = None


__all__ = [
    "ACCOUNT_PIN_MISMATCH",
    "APPLY_PREFLIGHT_REFUSED",
    "BROKER_UNCONFIGURED",
    "PROFILES_DATABASE_UNAVAILABLE",
    "RETIRED_ENVIRONMENT_SETTINGS",
    "BrokerUnbound",
    "UnboundBroker",
    "active_alpaca_binding_refusal",
    "get_active_alpaca_binding",
    "refuse_active_alpaca_binding",
    "reset_active_alpaca_binding_for_testing",
    "resolved_alpaca_settings",
    "set_active_alpaca_binding",
]
