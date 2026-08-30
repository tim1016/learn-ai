"""Regression tests for the data-lake catalog-truncation guard (#1887).

A prior overnight session pointed POSTGRES_URL at the shared dev container
(my-postgres) while running this suite. Nearly every `clean_artifacts`
fixture under tests/unit/data_lake and tests/integration/data_lake truncates
DataLakeArtifacts before and after each test it backs; run against the real
database, every one of those truncations wiped the real catalog to zero rows
(the on-disk lake files survived -- only the catalog index was destroyed).

tests/conftest.py's autouse `_guard_data_lake_catalog_truncation` fixture
exists to make that structurally impossible to repeat: it refuses to let any
fixture whose name starts with "clean_artifacts" run unless POSTGRES_URL has
been explicitly attested, out of band, as pointing at a disposable database.

These tests exercise the guard's decision logic directly -- the same
`_raise_if_catalog_truncation_is_unsafe` function the autouse fixture calls
-- rather than by driving a real fixture through pytest's injection
machinery, so they run everywhere (no POSTGRES_URL needed) and stay fast.
"""

from __future__ import annotations

import pytest

from app.config import settings
from tests.conftest import (
    _POSTGRES_TARGET_EPHEMERAL_ENV_VAR,
    _postgres_target_is_ephemeral,
    _raise_if_catalog_truncation_is_unsafe,
)

_FAKE_CONFIGURED_URL = "postgres://postgres:postgres@localhost:55432/postgres"


@pytest.fixture(autouse=True)
def _clear_ephemeral_attestation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's real shell may already export this. Every test in this
    file asserts the guard's behavior for a specific, explicit value, so
    start each one from "unset" rather than whatever the ambient shell has."""
    monkeypatch.delenv(_POSTGRES_TARGET_EPHEMERAL_ENV_VAR, raising=False)


@pytest.fixture
def postgres_url_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the state where a `clean_artifacts` fixture would actually
    reach a real database -- i.e. what every test in this file needs before
    it can assert the guard *blocks* anything. Patches the same attribute
    every per-file `_postgres_url()` helper reads first."""
    monkeypatch.setattr(settings, "POSTGRES_URL", _FAKE_CONFIGURED_URL)


def test_postgres_target_is_ephemeral_false_when_unset() -> None:
    assert _postgres_target_is_ephemeral() is False


@pytest.mark.parametrize("value", ["1", "true", "True", "TRUE"])
def test_postgres_target_is_ephemeral_true_for_accepted_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(_POSTGRES_TARGET_EPHEMERAL_ENV_VAR, value)
    assert _postgres_target_is_ephemeral() is True


@pytest.mark.parametrize("value", ["0", "false", "", "yes", "please"])
def test_postgres_target_is_ephemeral_false_for_rejected_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(_POSTGRES_TARGET_EPHEMERAL_ENV_VAR, value)
    assert _postgres_target_is_ephemeral() is False


def test_non_truncating_fixture_names_are_never_blocked(postgres_url_configured: None) -> None:
    # No fixture name here starts with "clean_artifacts" -- the guard has
    # nothing to do with this test, ephemeral attestation or not.
    _raise_if_catalog_truncation_is_unsafe(["pool", "tmp_lake", "client"])


def test_clean_artifacts_without_postgres_url_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No POSTGRES_URL configured means the fixture's own `_postgres_url()`
    helper will `pytest.skip` before issuing any SQL, exactly as it always
    has -- there is nothing yet for the guard to protect, so it must not
    preempt that skip with an unrelated "not ephemeral" error.

    Forces both lookups `_postgres_target_url()` falls back through to
    "unset" rather than trusting the ambient shell -- a developer (or this
    guard's own verification run, per #1887) may well have a real
    POSTGRES_URL exported while running the suite."""
    monkeypatch.setattr(settings, "POSTGRES_URL", "")
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    _raise_if_catalog_truncation_is_unsafe(["pool", "clean_artifacts"])


def test_clean_artifacts_without_ephemeral_attestation_raises(
    postgres_url_configured: None,
) -> None:
    with pytest.raises(RuntimeError, match=_POSTGRES_TARGET_EPHEMERAL_ENV_VAR):
        _raise_if_catalog_truncation_is_unsafe(["pool", "clean_artifacts"])


def test_clean_artifacts_prefix_variant_without_attestation_also_raises(
    postgres_url_configured: None,
) -> None:
    """Covers test_ensure_data_all_kinds.py's `clean_artifacts_all_kinds_complete`
    / `clean_artifacts_second_call` -- the guard matches by name *prefix*, not
    an exhaustive list, so the "clean_artifacts and equivalents" fixtures the
    issue describes are covered without editing this file again."""
    with pytest.raises(RuntimeError, match=_POSTGRES_TARGET_EPHEMERAL_ENV_VAR):
        _raise_if_catalog_truncation_is_unsafe(["clean_artifacts_all_kinds_complete"])


def test_clean_artifacts_with_ephemeral_attestation_does_not_raise(
    postgres_url_configured: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_POSTGRES_TARGET_EPHEMERAL_ENV_VAR, "1")
    _raise_if_catalog_truncation_is_unsafe(["pool", "clean_artifacts"])
