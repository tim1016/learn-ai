"""The environment variables the profiles database replaced, and how to read them.

Package F's half of ADR 0060: the six ``ALPACA_LIVE_*`` values and ``ALPACA_MODE``
stopped being the runtime's source of truth when an installation cut over to a
saved profile. This module owns three things that must not drift apart:

1. **Which variables are retired** — :data:`RETIRED_SETTINGS`. Exactly seven, and
   the list is closed. ``docs/architecture/alpaca-configuration-ownership-inventory.md``
   §F is the authority; :data:`NEVER_RETIRED_SETTINGS` records the other side of
   that table so the distinction is asserted rather than remembered.
2. **How to read them without ever refusing to construct** — the two readers
   below.
3. **The refusal their presence produces after cutover** —
   :func:`retired_environment_refusal`, stated once so the worker and the four
   operator CLIs answer a stale ``.env`` with the same sentence.

Why two readers rather than one, and why neither is ``AlpacaSettings``:

``AlpacaSettings`` requires a credential pair and enforces mode agreement, so
instantiating it to ask "is a retired variable still present?" would fail on
exactly the half-configured deployment that most needs the answer. Package C hit
the same wall for credentials and solved it the same way
(``AlpacaCredentialEnvironment`` — every field optional, never refuses).

:class:`LegacyEnvironmentPresence` types every field ``str | None`` so a *garbage*
value is still detected as present instead of raising. It is on the worker's boot
path, where an exception is a crash loop (#2014), so it must not have one.

:class:`LegacyEnvironmentValues` mirrors ``AlpacaSettings``' field *types* so a
parsed value is bit-identical to what the environment boot would have produced —
which is what makes the imported envelope's ``sha`` equal the legacy one. Per
CLAUDE.md guiding philosophy #5 this deliberate duplicate carries a parity test
naming its canonical file: ``tests/broker_configuration/test_legacy_environment.py``
pins every field and the resulting envelope ``sha`` against ``AlpacaSettings``.

Canonical implementation: ``app/broker/alpaca/config.py`` (``AlpacaSettings``)
  remains canonical for the *types and domain* of these values. This module is a
  reader of the same variables for one purpose — the cutover — and is the only
  admitted one besides the pre-cutover bootstrap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.broker.alpaca.active_binding import RETIRED_ENVIRONMENT_SETTINGS, UnboundBroker


@dataclass(frozen=True)
class RetiredSetting:
    """One environment variable the profiles database replaced."""

    env_var: str
    """The full variable name, as an operator sees it in ``.env``."""

    field: str
    """The ``AlpacaSettings`` field it populated — also this module's field name."""


_ENDPOINT_MODE = RetiredSetting(
    env_var="ALPACA_MODE",
    field="mode",
)

# The six risk-envelope values, in ``LiveEnvelopeValues`` field order so the
# mapping below is read top-to-bottom against the contract's §2.4 table.
_ENVELOPE_SETTINGS: Final[tuple[RetiredSetting, ...]] = (
    RetiredSetting(
        env_var="ALPACA_LIVE_LOSS_FRACTION",
        field="live_loss_fraction",
    ),
    RetiredSetting(
        env_var="ALPACA_LIVE_LOSS_USD",
        field="live_loss_usd",
    ),
    RetiredSetting(
        env_var="ALPACA_LIVE_SHADOW_SESSIONS",
        field="live_shadow_sessions",
    ),
    RetiredSetting(
        env_var="ALPACA_LIVE_ARMING_MAX_SESSIONS",
        field="live_arming_max_sessions",
    ),
    RetiredSetting(
        env_var="ALPACA_LIVE_XH_ENTRY_BPS",
        field="live_xh_entry_bps",
    ),
    RetiredSetting(
        env_var="ALPACA_LIVE_XH_EXIT_BPS",
        field="live_xh_exit_bps",
    ),
)

RETIRED_SETTINGS: Final[tuple[RetiredSetting, ...]] = (_ENDPOINT_MODE, *_ENVELOPE_SETTINGS)

RETIRED_ENV_VARS: Final[tuple[str, ...]] = tuple(setting.env_var for setting in RETIRED_SETTINGS)

# ``AlpacaSettings`` field name → ``LiveEnvelopeValues`` field name. The two
# differ only by the ``live_`` prefix and the ``live_loss_*`` → ``loss_*`` trim,
# and this is the one place that translation is written down.
ENVELOPE_FIELD_BY_SETTING: Final[dict[str, str]] = {
    "live_loss_fraction": "loss_fraction",
    "live_loss_usd": "loss_usd",
    "live_shadow_sessions": "shadow_sessions",
    "live_arming_max_sessions": "arming_max_sessions",
    "live_xh_entry_bps": "xh_entry_bps",
    "live_xh_exit_bps": "xh_exit_bps",
}

# The other side of the ownership inventory's §F table, recorded so the
# distinction is a test rather than a memory. **These are not retired and must
# keep working after cutover.** ``ALPACA_API_KEY_ID`` / ``ALPACA_API_SECRET_KEY``
# in particular *are* the ``default`` credential slot: refusing a boot because
# they are present would break every deployment, and it is the single easiest
# mistake to make in this package.
NEVER_RETIRED_SETTINGS: Final[tuple[str, ...]] = (
    # Secrets — a profile names an opaque slot, never a value (ADR 0060 D1).
    "ALPACA_API_KEY_ID",
    "ALPACA_API_SECRET_KEY",
    "ALPACA_CREDENTIAL_LIVE_KEY_ID",
    "ALPACA_CREDENTIAL_LIVE_SECRET_KEY",
    "ALPACA_QUALIFICATION_API_KEY_ID",
    "ALPACA_QUALIFICATION_API_SECRET_KEY",
    # Deployment bootstrap — must exist *before* a profile can be loaded.
    "ALPACA_CLERK_DIR",
    "ALPACA_CLERK_PRODUCTION_ACCOUNT_ID",
    "ALPACA_CLERK_UI_EVIDENCE_PATH",
    # Capability/release gates — never a profile permission switch.
    "ALPACA_SQLITE_MANUAL_TRADING_ENABLED",
    "ALPACA_FAULT_INJECTION_ENABLED",
    "ALPACA_PAPER_CARRYOVER_ENABLED",
)

_MODEL_CONFIG = SettingsConfigDict(
    env_file=".env",
    env_prefix="ALPACA_",
    case_sensitive=False,
    extra="ignore",
)


class LegacyEnvironmentPresence(BaseSettings):
    """Are the retired variables still set? Raw strings, so nothing can raise.

    Every field is ``str | None`` deliberately. This class answers a *presence*
    question on the worker's boot path, and a value that no longer parses —
    ``ALPACA_MODE=live-ish``, ``ALPACA_LIVE_LOSS_USD=oops`` — is still a stale
    line the operator must delete. Typing these as ``float`` would raise there
    and turn a refusal into the crash loop ADR 0060 Decision 7 forbids.
    """

    model_config = _MODEL_CONFIG

    mode: str | None = None
    live_loss_fraction: str | None = None
    live_loss_usd: str | None = None
    live_shadow_sessions: str | None = None
    live_arming_max_sessions: str | None = None
    live_xh_entry_bps: str | None = None
    live_xh_exit_bps: str | None = None


class LegacyEnvironmentValues(BaseSettings):
    """The retired values, typed exactly as ``AlpacaSettings`` types them.

    Field-for-field identical to ``AlpacaSettings``' declarations — same
    annotations, same ``Field`` constraints — with three deliberate differences:
    no credential pair, no ``_enforce_mode_agreement``, and ``mode`` optional
    here where it defaults to ``"paper"`` there, so that a *missing* mode is
    distinguishable from a chosen one. That is the whole point: a value parsed here is
    the value the environment boot would have used, so the envelope built from it
    hashes to the same ``sha``, and a *partial* live environment is reported
    field by field instead of refusing to construct at all.

    Pinned against the canonical declarations by
    ``tests/broker_configuration/test_legacy_environment.py``.
    """

    model_config = _MODEL_CONFIG

    mode: Literal["paper", "live"] | None = None
    live_loss_fraction: float | None = Field(default=None, gt=0, lt=1, allow_inf_nan=False)
    live_loss_usd: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    live_shadow_sessions: int | None = Field(default=None, ge=1)
    live_arming_max_sessions: int | None = Field(default=None, ge=1)
    live_xh_entry_bps: float | None = Field(default=None, ge=0, lt=10_000, allow_inf_nan=False)
    live_xh_exit_bps: float | None = Field(default=None, ge=0, lt=10_000, allow_inf_nan=False)


def current_retired_settings() -> LegacyEnvironmentPresence:
    """Read this process's environment for the retired variables.

    Both sources ``AlpacaSettings`` reads: the process environment *and* the
    ``.env`` file beside the working directory. A caller that already holds a
    reading passes it to the functions below instead of going through here, so
    one boot reads once.
    """
    return LegacyEnvironmentPresence()


def stale_retired_settings(
    presence: LegacyEnvironmentPresence | None = None,
) -> tuple[str, ...]:
    """The retired variables still present in the environment, by name.

    Names only — never values. A retired value is not a secret, but keeping this
    return type name-shaped means no caller can accidentally log one, and the
    operator only ever needs the name to delete the line.

    A variable set to an empty string counts as present: the line is still there,
    still describes a setting the runtime no longer reads, and the remedy is the
    same. "Blank it out" is not a supported way to retire one.
    """
    read = presence if presence is not None else current_retired_settings()
    return tuple(
        setting.env_var for setting in RETIRED_SETTINGS if getattr(read, setting.field) is not None
    )


def describe_stale_settings(stale: tuple[str, ...]) -> str:
    """One operator-readable sentence naming the stale variables."""
    if not stale:
        return ""
    listed = ", ".join(stale)
    if len(stale) == 1:
        return f"{listed} is still set in the environment"
    return f"{listed} are still set in the environment"


_FIELD_TO_ENV_VAR: Final[dict[str, str]] = {
    setting.field: setting.env_var for setting in RETIRED_SETTINGS
}


@dataclass(frozen=True)
class LegacyEnvironmentReadFailure:
    """The retired environment holds a value that will not parse.

    Each message names the ``ALPACA_*`` variable it came from and carries
    pydantic's ``msg`` text, never the offending input. The variable name comes
    from the error's ``loc`` — a *field* name, never a value — so naming it costs
    nothing in containment and is the whole difference between "Input should be a
    valid number" and an operator knowing which of seven lines to fix.
    """

    messages: tuple[str, ...]

    def describe(self) -> str:
        return "; ".join(self.messages) or "invalid legacy Alpaca configuration"


def retired_environment_refusal(
    presence: LegacyEnvironmentPresence | None = None,
) -> UnboundBroker | None:
    """The refusal a cut-over installation owes a stale retired variable, if any.

    ``None`` when the environment is clean. Callers ask this **only** once they
    know the installation has cut over — i.e. a profile revision is about to be
    bound. Before cutover the same variables are the legitimate and only source
    of the worker's configuration, and refusing on them would break every
    deployment that has not run the import yet.

    The prose lives here rather than at each call site so the worker and the
    operator CLIs answer a stale ``.env`` with the same sentence. It names the
    variables, because "which line do I delete?" is the only question an
    operator has at this point, and a generic error does not answer it.
    """
    stale = stale_retired_settings(presence)
    if not stale:
        return None
    return UnboundBroker(
        reason=RETIRED_ENVIRONMENT_SETTINGS,
        message=(
            f"{describe_stale_settings(stale)}, but this installation now reads its "
            "broker configuration from a saved profile. No broker is bound while a "
            "retired setting could still be mistaken for the one in force."
        ),
        next_step=(
            "Delete "
            + ", ".join(stale)
            + " from the environment file, then restart the service. The values "
            "themselves are already saved in the broker profile — removing the "
            "lines changes no configuration."
        ),
    )


def _named_messages(exc: ValidationError) -> tuple[str, ...]:
    """Pydantic's ``msg`` text, each prefixed with the variable it belongs to."""
    named: list[str] = []
    for error in exc.errors():
        message = str(error.get("msg", ""))
        if not message:
            continue
        location = error.get("loc") or ()
        field = str(location[0]) if location else ""
        env_var = _FIELD_TO_ENV_VAR.get(field)
        named.append(f"{env_var}: {message}" if env_var else message)
    return tuple(named)


def read_legacy_values(
    values: LegacyEnvironmentValues | None = None,
) -> LegacyEnvironmentValues | LegacyEnvironmentReadFailure:
    """Parse the retired variables, reporting a bad value rather than raising.

    The importer needs to *tell the operator* that ``ALPACA_LIVE_LOSS_USD`` is
    out of domain; a ``ValidationError`` escaping into a CLI traceback would say
    the same thing far less usefully, and on some paths would echo the input.
    """
    if values is not None:
        return values
    try:
        return LegacyEnvironmentValues()
    except ValidationError as exc:
        return LegacyEnvironmentReadFailure(messages=_named_messages(exc))


__all__ = [
    "ENVELOPE_FIELD_BY_SETTING",
    "NEVER_RETIRED_SETTINGS",
    "RETIRED_ENV_VARS",
    "RETIRED_SETTINGS",
    "LegacyEnvironmentPresence",
    "LegacyEnvironmentReadFailure",
    "LegacyEnvironmentValues",
    "RetiredSetting",
    "current_retired_settings",
    "describe_stale_settings",
    "read_legacy_values",
    "retired_environment_refusal",
    "stale_retired_settings",
]
