"""The retired/never-retired line, and the refusal a stale variable produces.

The distinction these tests pin is the single easiest thing to get wrong in the
cutover: ``ALPACA_API_KEY_ID`` and ``ALPACA_API_SECRET_KEY`` look like legacy
Alpaca variables and are named as such in casual descriptions of this work, but
they **are** the ``default`` credential slot. Refusing a boot because they are
present would break every deployment. The authority is
``docs/architecture/alpaca-configuration-ownership-inventory.md`` §F.

Every settings object here is built with ``_env_file=None`` so a developer's real
``PythonDataService/.env`` cannot decide the outcome — the same hazard the
autouse fixture in ``tests/conftest.py`` covers for the production readers.
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.active_binding import RETIRED_ENVIRONMENT_SETTINGS
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.broker.alpaca.config import _LIVE_REQUIRED_FIELDS, AlpacaSettings
from app.broker_configuration.envelope import ENVELOPE_FIELDS, ValidatedLiveEnvelope
from app.broker_configuration.legacy_environment import (
    ENVELOPE_FIELD_BY_SETTING,
    NEVER_RETIRED_SETTINGS,
    RETIRED_ENV_VARS,
    LegacyEnvironmentPresence,
    LegacyEnvironmentReadFailure,
    LegacyEnvironmentValues,
    current_retired_settings,
    read_legacy_values,
    retired_environment_refusal,
    stale_retired_settings,
)

# The full environment an ADR 0059 live deployment carries, and the one this
# whole package exists to move into a profile.
LEGACY_LIVE_ENVIRONMENT = {
    "ALPACA_MODE": "live",
    "ALPACA_LIVE_LOSS_FRACTION": "0.05",
    "ALPACA_LIVE_LOSS_USD": "5000",
    "ALPACA_LIVE_SHADOW_SESSIONS": "3",
    "ALPACA_LIVE_ARMING_MAX_SESSIONS": "20",
    "ALPACA_LIVE_XH_ENTRY_BPS": "11",
    "ALPACA_LIVE_XH_EXIT_BPS": "17.5",
}


def _presence(**overrides: str) -> LegacyEnvironmentPresence:
    return LegacyEnvironmentPresence(_env_file=None, **overrides)


def _values(**overrides: str) -> LegacyEnvironmentValues:
    return LegacyEnvironmentValues(_env_file=None, **overrides)


# ---- the registry ----------------------------------------------------------


def test_exactly_seven_settings_are_retired() -> None:
    """The endpoint mode and the six envelope values. Nothing else."""
    assert RETIRED_ENV_VARS == (
        "ALPACA_MODE",
        "ALPACA_LIVE_LOSS_FRACTION",
        "ALPACA_LIVE_LOSS_USD",
        "ALPACA_LIVE_SHADOW_SESSIONS",
        "ALPACA_LIVE_ARMING_MAX_SESSIONS",
        "ALPACA_LIVE_XH_ENTRY_BPS",
        "ALPACA_LIVE_XH_EXIT_BPS",
    )


@pytest.mark.parametrize("credential_variable", ["ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"])
def test_the_credential_pair_is_never_retired(credential_variable: str) -> None:
    """They *are* the ``default`` slot — retiring them would break every deployment."""
    assert credential_variable not in RETIRED_ENV_VARS
    assert credential_variable in NEVER_RETIRED_SETTINGS


def test_retired_and_never_retired_are_disjoint() -> None:
    assert not set(RETIRED_ENV_VARS) & set(NEVER_RETIRED_SETTINGS)


def test_the_envelope_mapping_covers_every_envelope_field() -> None:
    """The ``live_`` prefix trim is written down once, and it is complete."""
    assert set(ENVELOPE_FIELD_BY_SETTING.values()) == set(ENVELOPE_FIELDS)


# ---- presence detection ----------------------------------------------------


def test_a_clean_environment_has_nothing_stale() -> None:
    assert stale_retired_settings(_presence()) == ()


def test_a_retired_variable_is_detected_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_LIVE_LOSS_USD", "5000")

    assert stale_retired_settings(_presence()) == ("ALPACA_LIVE_LOSS_USD",)


def test_a_lowercase_spelling_is_still_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AlpacaSettings`` is ``case_sensitive=False``, so the detector must be too.

    An ``os.environ["ALPACA_MODE"]`` check would miss this and let a variable
    that really does reach ``AlpacaSettings`` pass as absent.
    """
    monkeypatch.setenv("alpaca_mode", "live")

    assert stale_retired_settings(_presence()) == ("ALPACA_MODE",)


def test_a_blanked_variable_still_counts_as_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blanking a line is not a supported way to retire it; deleting it is."""
    monkeypatch.setenv("ALPACA_MODE", "")

    assert stale_retired_settings(_presence()) == ("ALPACA_MODE",)


def test_an_unparseable_value_is_still_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Presence detection runs on the boot path and must never raise.

    ``ALPACA_LIVE_LOSS_USD=oops`` is a stale line like any other. Typing this
    reader as ``float`` would raise here and turn the refusal into the crash
    loop ADR 0060 Decision 7 forbids.
    """
    monkeypatch.setenv("ALPACA_LIVE_LOSS_USD", "oops")

    assert stale_retired_settings(_presence()) == ("ALPACA_LIVE_LOSS_USD",)


def test_credentials_and_bootstrap_produce_no_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The never-retired set must not trip the gate. This is the regression."""
    for name in NEVER_RETIRED_SETTINGS:
        monkeypatch.setenv(name, "present")

    assert stale_retired_settings(_presence()) == ()
    assert retired_environment_refusal(_presence()) is None


# ---- the refusal -----------------------------------------------------------


def test_the_refusal_names_every_stale_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_MODE", "live")
    monkeypatch.setenv("ALPACA_LIVE_LOSS_USD", "5000")

    refusal = retired_environment_refusal(_presence())

    assert refusal is not None
    assert refusal.reason == RETIRED_ENVIRONMENT_SETTINGS
    assert "ALPACA_MODE" in refusal.next_step
    assert "ALPACA_LIVE_LOSS_USD" in refusal.next_step
    # Actionable, not generic: the operator is told to delete the lines and that
    # doing so changes no configuration.
    assert "Delete" in refusal.next_step


def test_the_refusal_does_not_echo_a_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Names only. Nothing on this path needs a value, so nothing carries one."""
    monkeypatch.setenv("ALPACA_LIVE_LOSS_USD", "987654321")

    refusal = retired_environment_refusal(_presence())

    assert refusal is not None
    assert "987654321" not in refusal.message + refusal.next_step


# ---- parity with the canonical declarations --------------------------------


def test_legacy_values_declare_the_same_types_and_constraints() -> None:
    """CLAUDE.md guiding philosophy #5: the duplicate is pinned to its canonical file.

    Comparing *declarations*, not one parsed sample. A sample-based check stays
    green if `config.py` widens `live_xh_entry_bps` to `lt=20_000`, drops
    `allow_inf_nan=False`, or relaxes `gt=0` — and the duplicate would then
    admit a value the runtime would refuse, on the one path that carries a live
    limit out of the environment.
    """
    canonical = AlpacaSettings.model_fields
    ours = LegacyEnvironmentValues.model_fields

    for field in ENVELOPE_FIELD_BY_SETTING:
        assert ours[field].annotation == canonical[field].annotation, field
        assert _constraints(ours[field]) == _constraints(canonical[field]), field

    # ``mode`` is the one deliberate difference: optional here, defaulted to
    # "paper" there, because absence has to be distinguishable from a choice.
    assert ours["mode"].annotation is not canonical["mode"].annotation
    assert ours["mode"].default is None
    assert canonical["mode"].default == "paper"


def test_the_retired_envelope_set_matches_the_canonical_required_set() -> None:
    """`_LIVE_REQUIRED_FIELDS` is canon for which six values a live boot needs.

    If a seventh envelope value were ever added there, this module would keep
    importing six and the cutover would silently drop one.
    """
    assert set(_LIVE_REQUIRED_FIELDS) == set(ENVELOPE_FIELD_BY_SETTING)


def _constraints(field: object) -> list[str]:
    """A field's declared constraint metadata, comparably ordered."""
    return sorted(repr(item) for item in getattr(field, "metadata", ()))


def test_legacy_values_parse_exactly_as_alpaca_settings_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLAUDE.md guiding philosophy #5: the duplicate is pinned to its canonical file.

    ``LegacyEnvironmentValues`` mirrors ``AlpacaSettings``' field types so an
    imported value is the value the environment boot would have used. If the two
    ever disagree — a widened bound, a changed annotation — the import would
    write something the runtime never would.
    """
    for name, value in LEGACY_LIVE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ALPACA_API_KEY_ID", "key")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "secret")

    canonical = AlpacaSettings(_env_file=None)
    legacy = _values()

    assert legacy.mode == canonical.mode
    for settings_field in ENVELOPE_FIELD_BY_SETTING:
        theirs = getattr(canonical, settings_field)
        ours = getattr(legacy, settings_field)
        assert ours == theirs, settings_field
        assert type(ours) is type(theirs), settings_field


def test_the_imported_envelope_hashes_to_the_legacy_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0060 Decision 6, on the path that carries a live limit out of ``.env``.

    ``5000`` and ``5000.0`` are different envelope documents and every arming
    record already in the ledger is sealed over the latter. The environment
    string ``"5000"`` must therefore reach the profile as a ``float``.
    """
    for name, value in LEGACY_LIVE_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ALPACA_API_KEY_ID", "key")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "secret")

    canonical_sha = LiveEnvelopeValues.from_settings(AlpacaSettings(_env_file=None)).sha
    legacy = _values()
    imported = ValidatedLiveEnvelope.from_mapping(
        {
            envelope_field: getattr(legacy, settings_field)
            for settings_field, envelope_field in ENVELOPE_FIELD_BY_SETTING.items()
        }
    )

    assert imported.sha == canonical_sha
    assert type(imported.loss_usd) is float
    assert type(imported.shadow_sessions) is int


# ---- reading a broken environment ------------------------------------------


def test_an_out_of_domain_value_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """The importer has to *tell* the operator which value is wrong."""
    monkeypatch.setenv("ALPACA_LIVE_LOSS_FRACTION", "1.5")

    failure = read_legacy_values()

    assert isinstance(failure, LegacyEnvironmentReadFailure)
    assert failure.describe()


def test_a_read_failure_names_the_variable_it_came_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"Input should be a valid number" is unactionable across seven lines.

    The name comes from the error's `loc`, which is a field name and never a
    value, so naming it costs nothing in containment.
    """
    monkeypatch.setenv("ALPACA_LIVE_LOSS_USD", "oops")

    failure = read_legacy_values()

    assert isinstance(failure, LegacyEnvironmentReadFailure)
    assert "ALPACA_LIVE_LOSS_USD" in failure.describe()


def test_a_read_failure_does_not_echo_the_offending_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same containment ``alpaca_configuration_error_detail`` applies: ``msg`` only."""
    monkeypatch.setenv("ALPACA_LIVE_LOSS_USD", "sensitive-looking-garbage")

    failure = read_legacy_values()

    assert isinstance(failure, LegacyEnvironmentReadFailure)
    assert "sensitive-looking-garbage" not in failure.describe()


# ---- the .env file half of the reader --------------------------------------


def test_the_real_reader_sees_a_retired_line_in_a_dotenv_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half `tests/conftest.py` drops, covered deliberately in a tmp path.

    `AlpacaSettings` declares `env_file=".env"`, so a retired line living only in
    `PythonDataService/.env` — never exported into the process environment —
    still reaches it. The detector must see the same line, or an operator whose
    `.env` still holds `ALPACA_LIVE_LOSS_USD` would be told the environment is
    clean while `AlpacaSettings` disagreed.

    This is the one test that calls the *unpatched* `current_retired_settings`,
    so `env_file` drifting away from `AlpacaSettings`' would fail here and
    nowhere else.
    """
    (tmp_path / ".env").write_text("ALPACA_LIVE_LOSS_USD=5000\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for name in RETIRED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    assert stale_retired_settings(current_retired_settings()) == ("ALPACA_LIVE_LOSS_USD",)


def test_the_real_reader_reports_a_clean_dotenv_as_clean(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("ALPACA_API_KEY_ID=key\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for name in RETIRED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    assert stale_retired_settings(current_retired_settings()) == ()


def test_an_injected_reading_is_returned_untouched() -> None:
    supplied = _values(mode="paper")

    assert read_legacy_values(supplied) is supplied
