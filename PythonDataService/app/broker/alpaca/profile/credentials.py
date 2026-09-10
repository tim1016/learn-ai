"""The credential slot allowlist and resolver (contract §3).

A profile revision references an **opaque slot name**. This module is the only
thing that knows what a slot name means, and the mapping is a **fixed,
code-owned closed set** — the owner's resolution of ADR 0060 open question 2.
It is not read from the environment, not declared by a deployment, and not
derivable from caller input:

===========  ==========================================  ============================================
slot         key id                                      secret key
===========  ==========================================  ============================================
``default``  ``ALPACA_API_KEY_ID``                       ``ALPACA_API_SECRET_KEY``
``live``     ``ALPACA_CREDENTIAL_LIVE_KEY_ID``           ``ALPACA_CREDENTIAL_LIVE_SECRET_KEY``
===========  ==========================================  ============================================

``default`` is the compatibility slot: it is today's unrenamed pair, so every
current deployment keeps working untouched. ``live`` is the second injected
pair, so a paper profile and a live profile can both be provisioned at once and
switching which profile is effective does not mean editing ``.env``.

Two properties this module exists to guarantee:

- **A slot name never becomes a lookup.** :func:`resolve_credentials` refuses an
  unknown name *before* touching the environment object, so a profile cannot
  name an arbitrary variable or enumerate the process environment. The mapping
  is over pre-declared fields of :class:`AlpacaCredentialEnvironment`, never
  over a string the caller composed.
- **No secret crosses a boundary.** Values are held as ``SecretStr``, whose
  ``repr``/``str`` are masked and which raises rather than serialising through
  ``json.dumps``. :class:`CredentialSlotAvailability` — the only shape the
  contract's ``GET /credential-slots`` route may return — carries a label and a
  boolean and nothing else: not a value, not a fragment, not a length, and not
  a variable name.

Resolution happens at context construction only (contract §3), never per tick
and never in a router.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.broker.alpaca.profile.errors import (
    CredentialSlotUnavailable,
    CredentialSlotUnknown,
)

CREDENTIAL_SLOT_DEFAULT: Final = "default"
CREDENTIAL_SLOT_LIVE: Final = "live"

CredentialSlot = Literal["default", "live"]

# slot → the two ``AlpacaCredentialEnvironment`` field names holding its pair.
# Field names, not environment-variable names: the settings class owns the
# ``ALPACA_`` prefix, so no code path here concatenates a variable name.
_SLOT_FIELDS: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        CREDENTIAL_SLOT_DEFAULT: ("api_key_id", "api_secret_key"),
        CREDENTIAL_SLOT_LIVE: ("credential_live_key_id", "credential_live_secret_key"),
    }
)

CREDENTIAL_SLOTS: Final[tuple[str, ...]] = tuple(_SLOT_FIELDS)


class AlpacaCredentialEnvironment(BaseSettings):
    """Every injected Alpaca credential pair, read from the environment only.

    Deliberately separate from ``config.AlpacaSettings``: that type requires a
    key pair and enforces live mode agreement, which would make merely *asking
    whether a slot is available* fail on an unconfigured deployment. Every
    field here is optional, so this class never refuses to construct.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ALPACA_",
        case_sensitive=False,
        extra="ignore",
    )

    # The ``default`` (compatibility) slot — today's unrenamed pair.
    api_key_id: SecretStr | None = None
    api_secret_key: SecretStr | None = None
    # The ``live`` slot — ALPACA_CREDENTIAL_LIVE_KEY_ID / _SECRET_KEY.
    credential_live_key_id: SecretStr | None = None
    credential_live_secret_key: SecretStr | None = None


@dataclass(frozen=True)
class ResolvedCredentials:
    """One slot's injected pair, resolved for exactly one runtime binding.

    Held as ``SecretStr`` so the dataclass ``repr`` — and therefore any log
    line, traceback frame, or f-string that reaches one — shows a mask.
    :meth:`key_id` / :meth:`secret_key` are the two deliberate call sites that
    unwrap the value, at the point of handing it to the SDK.
    """

    slot: str
    api_key_id: SecretStr
    api_secret_key: SecretStr

    def key_id(self) -> str:
        """The plain key id, for handing to the vendor SDK. Never log this."""
        return self.api_key_id.get_secret_value()

    def secret_key(self) -> str:
        """The plain secret key, for handing to the vendor SDK. Never log this."""
        return self.api_secret_key.get_secret_value()


@dataclass(frozen=True)
class CredentialSlotAvailability:
    """The only credential fact that may cross the API boundary (contract §3).

    A label and a boolean. Availability is binary on purpose: reporting *which
    half* of a pair is missing would leak a fact about the injected secrets
    that the contract does not admit.
    """

    slot: str
    available: bool


def is_known_credential_slot(slot: object) -> bool:
    """Whether ``slot`` is on the closed allowlist. Reads no environment."""
    return isinstance(slot, str) and slot in _SLOT_FIELDS


def require_known_credential_slot(slot: object) -> str:
    """Return ``slot`` when allowlisted, else raise :class:`CredentialSlotUnknown`.

    This is the gate every other function in this module passes through before
    an environment object is consulted at all.
    """
    if not is_known_credential_slot(slot):
        raise CredentialSlotUnknown(known_slots=CREDENTIAL_SLOTS)
    return str(slot)


def _environment(environment: AlpacaCredentialEnvironment | None) -> AlpacaCredentialEnvironment:
    return environment if environment is not None else AlpacaCredentialEnvironment()


def _injected_pair(
    environment: AlpacaCredentialEnvironment, slot: str
) -> tuple[SecretStr | None, SecretStr | None]:
    key_field, secret_field = _SLOT_FIELDS[slot]
    return getattr(environment, key_field), getattr(environment, secret_field)


def _is_present(value: SecretStr | None) -> bool:
    """Whether a half of a pair was actually injected.

    A blank or whitespace-only value is a placeholder line in ``.env``, not a
    credential — treating it as present would send an empty key to the broker
    and surface as an auth failure instead of an honest "not configured".
    """
    return value is not None and bool(value.get_secret_value().strip())


def credential_slot_available(
    slot: object,
    *,
    environment: AlpacaCredentialEnvironment | None = None,
) -> bool:
    """Whether both halves of ``slot``'s pair are injected.

    Raises :class:`CredentialSlotUnknown` for a slot off the allowlist, so an
    unrecognised name is a refusal rather than a quiet ``False``.
    """
    known = require_known_credential_slot(slot)
    key_id, secret_key = _injected_pair(_environment(environment), known)
    return _is_present(key_id) and _is_present(secret_key)


def describe_credential_slots(
    *,
    environment: AlpacaCredentialEnvironment | None = None,
) -> tuple[CredentialSlotAvailability, ...]:
    """Every allowlisted slot with its availability — the ``GET`` route's body.

    Ordered by the allowlist so the surface is stable, and complete: an
    unavailable slot is still listed, because "this deployment has no live pair
    injected" is exactly what the operator needs to see.
    """
    resolved = _environment(environment)
    return tuple(
        CredentialSlotAvailability(
            slot=slot,
            available=credential_slot_available(slot, environment=resolved),
        )
        for slot in CREDENTIAL_SLOTS
    )


def resolve_credentials(
    slot: object,
    *,
    environment: AlpacaCredentialEnvironment | None = None,
) -> ResolvedCredentials:
    """Resolve one allowlisted slot into its injected pair.

    Raises :class:`CredentialSlotUnknown` (422) for a name off the allowlist —
    checked before any environment object is built or read — and
    :class:`CredentialSlotUnavailable` (409) when the slot is recognised but
    its pair was never injected.

    Deliberately takes no ``mode``: a slot maps to a pair regardless of
    endpoint mode. Which mode a resolved pair may be used under is the
    runtime context's question (``profile.runtime_context``) and the observed
    account's answer (``profile.account_verification``), and keeping the two
    apart is what lets one deployment hold a paper pair and a live pair at
    once.
    """
    known = require_known_credential_slot(slot)
    key_id, secret_key = _injected_pair(_environment(environment), known)
    if key_id is None or secret_key is None:
        raise CredentialSlotUnavailable(known)
    if not (_is_present(key_id) and _is_present(secret_key)):
        raise CredentialSlotUnavailable(known)
    return ResolvedCredentials(slot=known, api_key_id=key_id, api_secret_key=secret_key)


__all__ = [
    "CREDENTIAL_SLOTS",
    "CREDENTIAL_SLOT_DEFAULT",
    "CREDENTIAL_SLOT_LIVE",
    "AlpacaCredentialEnvironment",
    "CredentialSlot",
    "CredentialSlotAvailability",
    "ResolvedCredentials",
    "credential_slot_available",
    "describe_credential_slots",
    "is_known_credential_slot",
    "require_known_credential_slot",
    "resolve_credentials",
]
