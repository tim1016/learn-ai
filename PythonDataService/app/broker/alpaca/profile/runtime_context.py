"""The immutable runtime binding a worker resolves once (contract §7).

:func:`resolve_runtime_context` takes a profile revision's **non-secret**
values plus the name of a credential slot, and returns one frozen
:class:`AlpacaRuntimeContext`. That context is what Package D hands to broker,
client, stream and Clerk construction instead of each of them reaching for the
process-wide environment singleton.

Two design rules earn their keep here.

**Validation is not re-implemented.** ADR 0060 Decision 6 requires the domain
checks that live only in ``AlpacaSettings`` today — ``loss_fraction`` in (0, 1),
``loss_usd`` > 0, both counts ``int`` ≥ 1, both bps in [0, 10000), everything
finite — to move into *one* validated type that is the only constructor of
``LiveEnvelopeValues`` from stored data. Rather than restate those bounds in a
second model (two authorities that would drift), the resolved context builds an
``AlpacaSettings`` from explicit keyword arguments and lets it validate. So
``LiveEnvelopeValues.from_settings`` remains the single construction site, and
a stored revision and an environment-configured one produce a bit-identical
``sha`` by construction rather than by a matching pair of validators.

**Type fidelity is checked before Pydantic sees the value.** Pydantic's lax
mode coerces ``True`` → ``1`` and ``"3"`` → ``3``, which would silently accept a
row that violates the contract's §2.4 fidelity rule. :func:`resolve_runtime_context`
therefore asks each stored value for its Python type by name first — using the
same ``type(value) is int`` test ``live_arming`` applies to a sealed record —
and refuses anything else as ``revision_incomplete``. An ``int`` supplied for a
``float`` field is accepted and normalised to ``float``, which is precisely the
"convert explicitly on load" the contract asks for.

What this module deliberately does **not** take: an API base URL (derived from
mode, contract §2.3), an environment-variable name, or the Clerk directory. The
last is deployment bootstrap and stays an environment read inside
``AlpacaSettings``, so a profile can never relocate the custody volume.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, get_type_hints

from pydantic import ValidationError

from app.broker.alpaca.clerk.live_envelope import (
    ENVELOPE_SETTINGS_FIELDS,
    LiveEnvelopeValues,
)
from app.broker.alpaca.config import AlpacaSettings, alpaca_configuration_error_detail
from app.broker.alpaca.profile.credentials import (
    AlpacaCredentialEnvironment,
    ResolvedCredentials,
    resolve_credentials,
)
from app.broker.alpaca.profile.errors import RevisionIncomplete

EndpointMode = Literal["paper", "live"]

# The six envelope field names, exactly as ``LiveEnvelopeValues`` declares them
# — which is exactly what the contract's ``live_envelope`` block carries, so the
# mapping between the two is an identity and no rename layer can drift (§2.4).
LIVE_ENVELOPE_FIELDS: Final[tuple[str, ...]] = tuple(
    field for field, _ in ENVELOPE_SETTINGS_FIELDS
)

# The counts that must round-trip as exactly ``int``; every other field is a
# float. Derived from the dataclass's own annotations rather than hand-listed:
# a hand-listed split would drift silently if a field's type changed or a
# seventh were added, and drift here changes the ``sha`` — which every arming
# record already in an operator's ledger is sealed over (contract §2.4).
_INTEGER_ENVELOPE_FIELDS: Final[frozenset[str]] = frozenset(
    name
    for name, hint in get_type_hints(LiveEnvelopeValues).items()
    if hint is int
)

# What a revision with no live envelope hands ``AlpacaSettings``: every live
# field explicitly ``None``, so a stale ``ALPACA_LIVE_*`` in the environment
# cannot contribute a value to a resolved revision (ADR 0060 Decision 7 — there
# is no fallback to a stale user setting after cutover).
_ABSENT_ENVELOPE_SETTINGS: Final[dict[str, None]] = {
    settings_field: None for _, settings_field in ENVELOPE_SETTINGS_FIELDS
}


def is_exactly_int(value: object) -> bool:
    """Whether ``value`` is an ``int`` and nothing that merely behaves like one.

    ``True`` is an ``int`` to ``isinstance`` and ``1.0`` compares equal to
    ``1``, so the type is asked for by name.

    **Duplicate, with a parity test.** ``clerk/live_arming.py::_is_int`` is the
    canonical statement of this predicate for a sealed arming record, and this
    file deliberately does not import it: ``live_arming`` pulls
    ``app.lean_sidecar.trading_calendar`` and with it the market-calendar
    dependency, which has no business on the credential-resolution path. The
    parity test that pins the two against each other is
    ``tests/broker/alpaca/profile/test_runtime_context.py::
    test_the_integer_predicate_agrees_with_the_sealed_record_validator``
    (CLAUDE.md guiding philosophy #5).
    """
    return type(value) is int


def _is_real_number(value: object) -> bool:
    """Whether ``value`` is an ``int`` or ``float`` — and not a ``bool``."""
    return type(value) is int or type(value) is float


def _envelope_settings(live_envelope: Mapping[str, object]) -> dict[str, float | int]:
    """The stored envelope as ``AlpacaSettings`` keyword arguments.

    Checks the keys and the Python types on the way (contract §2.4), and keys
    the result by settings field so the caller does not walk the same pairing a
    second time.
    """
    supplied = set(live_envelope)
    expected = set(LIVE_ENVELOPE_FIELDS)
    missing = sorted(expected - supplied)
    if missing:
        raise RevisionIncomplete("its live envelope is missing " + ", ".join(missing))
    unexpected = sorted(supplied - expected)
    if unexpected:
        raise RevisionIncomplete(
            "its live envelope carries values that are not envelope fields: "
            + ", ".join(unexpected)
        )

    values: dict[str, float | int] = {}
    for field, settings_field in ENVELOPE_SETTINGS_FIELDS:
        value = live_envelope[field]
        if field in _INTEGER_ENVELOPE_FIELDS:
            if not is_exactly_int(value):
                raise RevisionIncomplete(f"its {field} is not stored as a whole number")
            values[settings_field] = value
            continue
        if not _is_real_number(value):
            raise RevisionIncomplete(f"its {field} is not stored as a number")
        values[settings_field] = float(value)
    return values


@dataclass(frozen=True)
class AlpacaRuntimeContext:
    """One resolved, immutable broker binding.

    ``settings`` is a fully-specified ``AlpacaSettings`` — the same type every
    existing consumer already accepts — built from this revision's values
    rather than from the process environment. Package D's migration is
    therefore ``get_alpaca_settings()`` → ``context.settings`` at each call
    site, and ``mode`` / ``is_paper`` / ``is_live`` / ``base_url`` are read
    through it, keeping ``AlpacaSettings`` the single place the endpoint is
    derived from the mode.

    ``profile_id`` and ``revision`` are provenance for the surfaces that report
    staged-versus-effective; they are never execution inputs, and a change to
    either alone alters no execution identity.
    """

    settings: AlpacaSettings
    credentials: ResolvedCredentials
    live_envelope: LiveEnvelopeValues | None
    account_pin: str | None = None
    profile_id: str | None = None
    revision: int | None = None

    @property
    def credential_slot(self) -> str:
        """The slot label this binding resolved through — never its variables."""
        return self.credentials.slot

    def __repr__(self) -> str:
        """Identify the binding without reproducing ``AlpacaSettings``.

        Pydantic's own ``repr`` would print every settings field. The two
        credential fields are declared ``repr=False`` there, but the safe thing
        for a context that reaches log lines and traceback frames is to name
        only the facts a surface may show anyway.
        """
        return (
            f"AlpacaRuntimeContext(profile_id={self.profile_id!r}, "
            f"revision={self.revision!r}, mode={self.settings.mode!r}, "
            f"credential_slot={self.credential_slot!r}, "
            f"account_pin={self.account_pin!r})"
        )


def resolve_runtime_context(
    *,
    endpoint_mode: EndpointMode,
    credential_slot: str,
    live_envelope: Mapping[str, object] | None = None,
    account_pin: str | None = None,
    profile_id: str | None = None,
    revision: int | None = None,
    environment: AlpacaCredentialEnvironment | None = None,
) -> AlpacaRuntimeContext:
    """Resolve one profile revision into an immutable runtime binding.

    ``live_envelope`` carries the six values under ``LiveEnvelopeValues``' own
    field names, or is ``None`` on a paper revision that declares none. A
    ``live`` endpoint mode without a complete envelope is refused as
    ``revision_incomplete`` (422) — the profile-world equivalent of today's
    ``_enforce_mode_agreement`` startup refusal, and never a process crash.

    Raises ``CredentialSlotUnknown`` (422) / ``CredentialSlotUnavailable``
    (409) from the slot resolver, and :class:`RevisionIncomplete` (422) for a
    revision whose own values cannot make a runtime.
    """
    if endpoint_mode not in ("paper", "live"):
        raise RevisionIncomplete("its endpoint mode is neither 'paper' nor 'live'")
    if endpoint_mode == "live" and live_envelope is None:
        # Refused here, in profile vocabulary, rather than by letting
        # ``_enforce_mode_agreement`` refuse downstream: that validator's
        # message names six environment variables and says "Refusing to start",
        # which is ADR 0059's environment-source rule — exactly the rule
        # ADR 0060 supersedes — and contract §6 renders ``message`` verbatim.
        raise RevisionIncomplete(
            "a live revision carries all six live envelope values and this one "
            "carries none"
        )

    credentials = resolve_credentials(credential_slot, environment=environment)
    envelope_settings = (
        _ABSENT_ENVELOPE_SETTINGS
        if live_envelope is None
        else _envelope_settings(live_envelope)
    )

    try:
        settings = AlpacaSettings(
            # The ``SecretStr`` objects go in unwrapped-never. Pydantic captures
            # the *raw input* in a failed validation's ``input_value``, and for
            # a model-level validator that input is the whole kwargs dict with
            # ``loc == ()`` — so no per-field inspection could have filtered it.
            # Handing it ``SecretStr`` means the worst case renders
            # ``SecretStr('**********')``.
            api_key_id=credentials.api_key_id,
            api_secret_key=credentials.api_secret_key,
            mode=endpoint_mode,
            **envelope_settings,
        )
    except ValidationError as exc:
        # ...and the chain is severed regardless, so no traceback frame can
        # carry the input at all. ``alpaca_configuration_error_detail`` keeps
        # only Pydantic's ``msg`` text, which never echoes the input, so the
        # detail below is the whole diagnostic a caller needs.
        raise RevisionIncomplete(alpaca_configuration_error_detail(exc)) from None

    return AlpacaRuntimeContext(
        settings=settings,
        credentials=credentials,
        live_envelope=(
            None if live_envelope is None else LiveEnvelopeValues.from_settings(settings)
        ),
        account_pin=account_pin,
        profile_id=profile_id,
        revision=revision,
    )


__all__ = [
    "LIVE_ENVELOPE_FIELDS",
    "AlpacaRuntimeContext",
    "EndpointMode",
    "is_exactly_int",
    "resolve_runtime_context",
]
