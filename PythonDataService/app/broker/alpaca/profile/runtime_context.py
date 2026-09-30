"""Resolve one immutable profile revision without environment fallback.

Current profiles carry four monetary fields. Historical revisions also store
both retired integer session counts, solely so the resolved envelope keeps
its original sealed identity. They are type-checked here and carried from the
stored revision straight into the envelope: no setting holds them any more
(#2629), so a stale environment count has nothing to reach.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import ValidationError

from app.broker.alpaca.clerk.live_envelope import (
    ENVELOPE_SETTINGS_FIELDS,
    RETIRED_ENVELOPE_FIELDS,
    LiveEnvelopeValues,
    envelope_domain_violation,
)
from app.broker.alpaca.config import AlpacaSettings, alpaca_configuration_error_detail
from app.broker.alpaca.profile.credentials import (
    AlpacaCredentialEnvironment,
    ResolvedCredentials,
    resolve_credentials,
)
from app.broker.alpaca.profile.errors import RevisionIncomplete
from app.schemas.exit_terms import ExitTermsInput

EndpointMode = Literal["paper", "live"]

# The six envelope field names, exactly as ``LiveEnvelopeValues`` declares them
# — which is exactly what the contract's ``live_envelope`` block carries, so the
# mapping between the two is an identity and no rename layer can drift (§2.4).
LIVE_ENVELOPE_FIELDS: Final[tuple[str, ...]] = (
    *(field for field, _ in ENVELOPE_SETTINGS_FIELDS),
    *RETIRED_ENVELOPE_FIELDS,
)

# What a revision with no live envelope hands ``AlpacaSettings``: every live
# field explicitly ``None``, so a stale ``ALPACA_LIVE_*`` in the environment
# cannot contribute a value to a resolved revision (ADR 0060 Decision 7 — there
# is no fallback to a stale user setting after cutover).
_ABSENT_ENVELOPE_SETTINGS: Final[dict[str, None]] = {
    settings_field: None for _, settings_field in ENVELOPE_SETTINGS_FIELDS
}

# The two envelope fields a paper revision may carry on its own (#2440, owner
# decision 2026-09-25), and the settings fields they bind. A paper binding's
# pair reaches the pricing policy through exactly these settings —
# ``ExtendedHoursAllowances.from_settings`` reads them for a paper or ``sim:``
# authority, just as a pre-cutover paper worker read them from the
# environment — so no pricing code learns a second source. Selected from the
# envelope's own table so the settings names cannot drift from it; the key
# pair is pinned equal to ``app/broker_configuration/envelope.py``'s
# ``ALLOWANCE_FIELDS`` by ``tests/broker/alpaca/profile/test_runtime_context.py``.
PAPER_ALLOWANCE_SETTINGS_FIELDS: Final[tuple[tuple[str, str], ...]] = tuple(
    pair for pair in ENVELOPE_SETTINGS_FIELDS if pair[0] in ("xh_entry_bps", "xh_exit_bps")
)


def is_exactly_int(value: object) -> bool:
    """Whether ``value`` is an ``int`` and nothing that merely behaves like one.

    ``True`` is an ``int`` to ``isinstance`` and ``1.0`` compares equal to
    ``1``, so the type is asked for by name.

    **Duplicate, with a parity test.** ``clerk/live_arming.py::_is_int`` is the
    canonical statement of this predicate for a sealed arming record, and this
    file deliberately does not import it: the historical arming-record module
    has no business on the credential-resolution path. The parity test that
    pins the two against each other is
    ``tests/broker/alpaca/profile/test_runtime_context.py::
    test_the_integer_predicate_agrees_with_the_sealed_record_validator``
    (CLAUDE.md guiding philosophy #5).
    """
    return type(value) is int


def _is_real_number(value: object) -> bool:
    """Whether ``value`` is an ``int`` or ``float`` — and not a ``bool``."""
    return type(value) is int or type(value) is float


def _envelope_settings(live_envelope: Mapping[str, object]) -> dict[str, float]:
    """The stored envelope as ``AlpacaSettings`` keyword arguments.

    Checks the keys and the Python types on the way (contract §2.4), and keys
    the result by settings field so the caller does not walk the same pairing a
    second time. A historical revision's two session counts are type-checked
    here and carried into the envelope by the caller, whose domain check
    covers them: no setting holds them (#2629).
    """
    supplied = set(live_envelope)
    expected = set(LIVE_ENVELOPE_FIELDS)
    current = {field for field, _ in ENVELOPE_SETTINGS_FIELDS}
    missing = sorted(current - supplied)
    if missing:
        raise RevisionIncomplete("its live envelope is missing " + ", ".join(missing))
    unexpected = sorted(supplied - expected)
    if unexpected:
        raise RevisionIncomplete(
            "its live envelope carries values that are not envelope fields: "
            + ", ".join(unexpected)
        )

    if supplied not in (current, expected):
        raise RevisionIncomplete("its historical envelope must carry both retired session counts")
    for field in RETIRED_ENVELOPE_FIELDS:
        if field not in live_envelope:
            continue
        if not is_exactly_int(live_envelope[field]):
            raise RevisionIncomplete(f"its {field} is not stored as a whole number")
    values: dict[str, float] = {}
    for field, settings_field in ENVELOPE_SETTINGS_FIELDS:
        value = live_envelope[field]
        if not _is_real_number(value):
            raise RevisionIncomplete(f"its {field} is not stored as a number")
        values[settings_field] = float(value)
    return values


def _paper_allowance_settings(paper_xh_allowances: Mapping[str, object]) -> dict[str, float]:
    """A paper revision's own pair as ``AlpacaSettings`` keyword arguments.

    The same key and type checks as the envelope's, for the same reason: a
    stored value that is not a real number must refuse, not coerce.
    """
    expected = {field for field, _ in PAPER_ALLOWANCE_SETTINGS_FIELDS}
    if set(paper_xh_allowances) != expected:
        raise RevisionIncomplete(
            "its paper allowances must be exactly "
            + " and ".join(sorted(expected))
        )
    values: dict[str, float] = {}
    for field, settings_field in PAPER_ALLOWANCE_SETTINGS_FIELDS:
        value = paper_xh_allowances[field]
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

    default_exit_terms: ExitTermsInput | None = None

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
    paper_xh_allowances: Mapping[str, object] | None = None,
    default_exit_terms: ExitTermsInput | None = None,
    account_pin: str | None = None,
    profile_id: str | None = None,
    revision: int | None = None,
    environment: AlpacaCredentialEnvironment | None = None,
) -> AlpacaRuntimeContext:
    """Resolve one profile revision into an immutable runtime binding.

    ``live_envelope`` carries four current or six historical values under ``LiveEnvelopeValues``' own
    field names, or is ``None`` on a paper revision that declares none. A
    ``live`` endpoint mode without a complete envelope is refused as
    ``revision_incomplete`` (422) — the profile-world equivalent of today's
    ``_enforce_mode_agreement`` startup refusal, and never a process crash.

    ``paper_xh_allowances`` is a paper revision's own extended-hours pair
    (#2440), accepted only beside no envelope. It binds the two allowance
    settings and nothing else: the context's ``live_envelope`` stays ``None``,
    so a paper binding never composes a live envelope out of its allowances.

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
            "a live revision carries all four live envelope monetary values and this one "
            "carries none"
        )

    if paper_xh_allowances is not None and (endpoint_mode != "paper" or live_envelope is not None):
        # The store's CHECK refuses this row; a caller handing the pair in
        # beside an envelope would otherwise leave two answers to one price.
        raise RevisionIncomplete(
            "its extended-hours allowances are in two places; a live revision carries "
            "them in its envelope and a paper revision beside none"
        )

    credentials = resolve_credentials(credential_slot, environment=environment)
    envelope_settings: Mapping[str, float | None] = _ABSENT_ENVELOPE_SETTINGS
    if live_envelope is not None:
        envelope_settings = {**_ABSENT_ENVELOPE_SETTINGS, **_envelope_settings(live_envelope)}
    elif paper_xh_allowances is not None:
        envelope_settings = {
            **_ABSENT_ENVELOPE_SETTINGS,
            **_paper_allowance_settings(paper_xh_allowances),
        }

    try:
        settings = AlpacaSettings(
            # The ``SecretStr`` objects go in still wrapped, never unwrapped:
            # Pydantic captures the *raw input* in ``input_value``, and for
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

    envelope = None if live_envelope is None else LiveEnvelopeValues(
        **{field: getattr(settings, settings_field) for field, settings_field in ENVELOPE_SETTINGS_FIELDS},
        **{field: live_envelope[field] for field in RETIRED_ENVELOPE_FIELDS if field in live_envelope},
    )
    # Settings already refused a current value out of its domain; this reaches
    # the retired counts, which no setting holds.
    violation = None if envelope is None else envelope_domain_violation(envelope)
    if violation is not None:
        raise RevisionIncomplete(f"its {violation}")
    return AlpacaRuntimeContext(
        default_exit_terms=default_exit_terms,
        settings=settings,
        credentials=credentials,
        live_envelope=envelope,
        account_pin=account_pin,
        profile_id=profile_id,
        revision=revision,
    )


__all__ = [
    "LIVE_ENVELOPE_FIELDS",
    "PAPER_ALLOWANCE_SETTINGS_FIELDS",
    "AlpacaRuntimeContext",
    "EndpointMode",
    "is_exactly_int",
    "resolve_runtime_context",
]
