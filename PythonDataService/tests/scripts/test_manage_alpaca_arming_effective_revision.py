"""The arming CLI against the profiles database (ADR 0060 package D).

Three properties, and they are the ones a real-money mistake would go through:

* the ceremony's settings come from the installation's **effective** profile
  revision, resolved in the CLI's own process, with no environment fallback on a
  configured installation and no write of any kind on the read path;
* ``plan`` and ``apply`` refuse while a *different* revision is staged, naming
  both -- and ``status`` and ``disarm`` keep answering, because reading the state
  and revoking a permission are exactly what an operator needs while a change is
  pending;
* ``plan`` prints the before→after difference between the envelope it would seal
  and the one already sealed on the account.

Every root is under ``tmp_path``: no test here touches a real Clerk volume, a
running container, or an injected credential.
"""

from __future__ import annotations

import errno
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.broker.alpaca.active_binding import (
    BROKER_UNCONFIGURED,
    PROFILES_DATABASE_UNAVAILABLE,
    BrokerUnbound,
)
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.broker.alpaca.config import AlpacaSettings, reset_alpaca_settings_for_testing
from app.broker_configuration import runtime as broker_configuration_runtime
from app.broker_configuration.envelope import ValidatedLiveEnvelope
from app.broker_configuration.errors import ProfilesDatabaseUnavailable
from app.broker_configuration.runtime import (
    build_service,
    reset_broker_configuration_service_for_testing,
)
from app.broker_configuration.service import BrokerConfigurationService
from app.broker_configuration.store import profiles_database_path
from scripts.manage_alpaca_arming import (
    LIVE_ARMING_REVISION_STAGED,
    effective_broker,
    main,
)
from tests.broker.alpaca.clerk.live_arming_fixtures import (
    ARMED_AT_MS,
    ARMING_SID,
    arming_ready,
    live_settings,
)
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT, TEST_ENVELOPE_VALUES

SETTINGS = live_settings()

# The live credential slot, as package C's closed allowlist names it, and the
# two variables it maps to. Injected per test so nothing reads a developer's.
LIVE_SLOT = "live"
LIVE_KEY_ID_VAR = "ALPACA_CREDENTIAL_LIVE_KEY_ID"
LIVE_SECRET_VAR = "ALPACA_CREDENTIAL_LIVE_SECRET_KEY"

# A second live envelope that differs from ``TEST_ENVELOPE_VALUES`` in three of
# the six values, so a diff that reported the wrong count or the wrong fields
# could not pass.
OTHER_ENVELOPE = LiveEnvelopeValues(
    loss_fraction=TEST_ENVELOPE_VALUES.loss_fraction,
    loss_usd=7_500.0,
    shadow_sessions=TEST_ENVELOPE_VALUES.shadow_sessions,
    arming_max_sessions=5,
    xh_entry_bps=TEST_ENVELOPE_VALUES.xh_entry_bps,
    xh_exit_bps=25.0,
)


@pytest.fixture()
def roots(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "clerk", tmp_path / "runner"


def _flags(roots: tuple[Path, Path]) -> list[str]:
    artifacts_root, live_state_root = roots
    return ["--artifacts-root", str(artifacts_root), "--live-state-root", str(live_state_root)]


def _last_object(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out.splitlines()[-1])


@pytest.fixture()
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """An Alpaca environment that is entirely this test's.

    ``AlpacaSettings`` and ``AlpacaCredentialEnvironment`` both read ``.env``
    from the process working directory, so the chdir is what stops a developer's
    real configuration contributing a value to any of these assertions.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALPACA_API_KEY_ID", "key")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "secret")
    monkeypatch.setenv(LIVE_KEY_ID_VAR, "live-key")
    monkeypatch.setenv(LIVE_SECRET_VAR, "live-secret")
    # No ``ALPACA_MODE``: these tests make a revision effective, i.e. the
    # installation has cut over, and package F refuses to resolve a binding
    # while a retired variable is still set. Deleting it is what a real
    # cut-over deployment does, and ``AlpacaSettings.mode`` defaults to
    # "paper" regardless, so nothing else here changes.
    monkeypatch.delenv("ALPACA_MODE", raising=False)
    for suffix in (
        "LOSS_FRACTION",
        "LOSS_USD",
        "SHADOW_SESSIONS",
        "ARMING_MAX_SESSIONS",
        "XH_ENTRY_BPS",
        "XH_EXIT_BPS",
    ):
        monkeypatch.delenv(f"ALPACA_LIVE_{suffix}", raising=False)
    reset_alpaca_settings_for_testing()
    reset_broker_configuration_service_for_testing()
    yield
    reset_alpaca_settings_for_testing()
    reset_broker_configuration_service_for_testing()


@pytest.fixture()
def profiles(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, isolated_environment: None
) -> Iterator[BrokerConfigurationService]:
    """A real profiles database on the same Clerk root the arming ledger uses.

    Deliberately the real service with the real credential-slot directory: these
    tests resolve a revision for real, so a fake slot name or a fake resolver
    would prove nothing about what the CLI does on the volume.
    """
    clerk_dir, _live_state_root = roots
    clerk_dir.mkdir(parents=True, exist_ok=True)
    # ``tests/conftest.py`` already pins the profiles database inside tmp_path by
    # patching this resolver; patching it again (later patch wins, as that
    # fixture's docstring says) is how a test names its own location. The
    # environment variable is set too so ``AlpacaSettings.clerk_dir`` agrees --
    # in production the profiles database and the Clerk authority share one root.
    monkeypatch.setattr(broker_configuration_runtime, "resolve_clerk_dir", lambda: clerk_dir)
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(clerk_dir))
    reset_alpaca_settings_for_testing()
    reset_broker_configuration_service_for_testing()
    service = build_service(clerk_dir=clerk_dir)
    yield service
    service.close()


def _live_profile(
    service: BrokerConfigurationService,
    *,
    display_name: str,
    envelope: LiveEnvelopeValues,
) -> str:
    created = service.create_profile(
        display_name=display_name,
        credential_slot=LIVE_SLOT,
        endpoint_mode="live",
        live_envelope=ValidatedLiveEnvelope.from_mapping(envelope.to_mapping()),
    )
    return created.profile.profile_id


def _make_effective(service: BrokerConfigurationService, profile_id: str, revision: int) -> None:
    """Drive the selection to "this revision is effective", the way a worker does."""
    service.stage_selection(
        profile_id=profile_id,
        revision=revision,
        expected_selection_generation=service.selection().selection_generation,
    )
    service.acknowledge_effective(
        profile_id=profile_id,
        revision=revision,
        account_id=LIVE_ACCT,
        expected_selection_generation=service.selection().selection_generation,
    )


# ---- what the CLIs resolve -------------------------------------------------


def test_effective_broker_bootstraps_from_the_environment_before_cutover(
    isolated_environment: None, roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No profiles database at all is the pre-cutover state, not a refusal.

    And the read stays a read: resolving must not conjure an empty profiles
    database onto the Clerk volume, which ``ProfilesStore.open`` would.
    """
    clerk_dir, _live_state_root = roots
    monkeypatch.setattr(broker_configuration_runtime, "resolve_clerk_dir", lambda: clerk_dir)
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(clerk_dir))
    reset_alpaca_settings_for_testing()

    resolved = effective_broker()

    assert resolved.settings.mode == "paper"
    assert resolved.selection is None
    assert not profiles_database_path(clerk_dir).exists()


def test_effective_broker_refuses_a_configured_installation_with_nothing_applied(
    profiles: BrokerConfigurationService,
) -> None:
    """Profiles exist but none is effective: the gate closes, with no env fallback."""
    _live_profile(profiles, display_name="Live — primary", envelope=TEST_ENVELOPE_VALUES)

    with pytest.raises(BrokerUnbound) as refused:
        effective_broker()

    assert refused.value.reason == BROKER_UNCONFIGURED
    assert "Apply" in refused.value.unbound.next_step


def test_effective_broker_refuses_an_unreadable_profiles_database() -> None:
    def _refuse() -> BrokerConfigurationService:
        raise ProfilesDatabaseUnavailable("the volume is not mounted")

    with pytest.raises(BrokerUnbound) as refused:
        effective_broker(service_factory=_refuse)

    assert refused.value.reason == PROFILES_DATABASE_UNAVAILABLE


@pytest.mark.parametrize("broken_component", ["database", "directory"])
def test_effective_broker_refuses_broken_configuration_links(
    isolated_environment: None, roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
    broken_component: str,
) -> None:
    clerk_dir, _ = roots
    database = profiles_database_path(clerk_dir)
    target = database if broken_component == "database" else database.parent
    target.parent.mkdir(parents=True, exist_ok=True)
    missing = clerk_dir / "missing-target"
    target.symlink_to(missing)
    monkeypatch.setattr(broker_configuration_runtime, "resolve_clerk_dir", lambda: clerk_dir)

    with pytest.raises(BrokerUnbound) as refused:
        effective_broker()

    assert refused.value.reason == PROFILES_DATABASE_UNAVAILABLE
    assert not missing.exists()


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
def test_effective_broker_translates_configuration_stat_failures(
    isolated_environment: None, roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    clerk_dir, _ = roots
    database = profiles_database_path(clerk_dir)
    database.parent.mkdir(parents=True)
    monkeypatch.setattr(broker_configuration_runtime, "resolve_clerk_dir", lambda: clerk_dir)
    original_stat = Path.stat

    def unavailable(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if path == database:
            raise OSError(error_number, "configuration volume unavailable")
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", unavailable)
    with pytest.raises(BrokerUnbound) as refused:
        effective_broker()

    assert refused.value.reason == PROFILES_DATABASE_UNAVAILABLE


def test_effective_broker_never_consumes_a_pending_apply(
    profiles: BrokerConfigurationService,
) -> None:
    """Applying is the worker's job; a CLI reads the effective revision only.

    A CLI that ran the worker's ceremony would install a staged revision nobody
    restarted onto, or -- on a refusal -- record one and advance the selection
    generation under a browser holding it.
    """
    effective_id = _live_profile(profiles, display_name="Live — one", envelope=TEST_ENVELOPE_VALUES)
    _make_effective(profiles, effective_id, 1)
    staged_id = _live_profile(profiles, display_name="Live — two", envelope=OTHER_ENVELOPE)
    profiles.stage_selection(
        profile_id=staged_id,
        revision=1,
        expected_selection_generation=profiles.selection().selection_generation,
    )
    profiles.request_apply(
        expected_selection_generation=profiles.selection().selection_generation
    )
    before = profiles.selection()

    resolved = effective_broker()

    assert LiveEnvelopeValues.from_settings(resolved.settings) == TEST_ENVELOPE_VALUES
    after = profiles.selection()
    assert after.apply_requested is True
    assert after.selection_generation == before.selection_generation
    assert after.last_apply_outcome == before.last_apply_outcome
    assert (after.effective_profile_id, after.effective_revision) == (effective_id, 1)


def test_effective_broker_round_trips_the_six_values_to_todays_envelope_sha(
    profiles: BrokerConfigurationService,
) -> None:
    """Store → load → ``LiveEnvelopeValues.sha`` is unchanged, types included.

    ``5000`` and ``5000.0`` hash differently, so the two counts staying ``int``
    and the four rates staying ``float`` is the whole property: every arming
    record already in an operator's ledger is sealed over this sha.
    """
    profile_id = _live_profile(profiles, display_name="Live — primary", envelope=TEST_ENVELOPE_VALUES)
    _make_effective(profiles, profile_id, 1)

    settings = effective_broker().settings

    assert LiveEnvelopeValues.from_settings(settings).sha == TEST_ENVELOPE_VALUES.sha
    assert type(settings.live_shadow_sessions) is int
    assert type(settings.live_arming_max_sessions) is int
    assert type(settings.live_loss_usd) is float


# ---- arming the effective revision only ------------------------------------


def test_plan_arms_the_envelope_of_the_effective_revision(
    profiles: BrokerConfigurationService,
    roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End to end with no injected settings: the seal comes from the database."""
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    profile_id = _live_profile(profiles, display_name="Live — primary", envelope=TEST_ENVELOPE_VALUES)
    _make_effective(profiles, profile_id, 1)

    assert (
        main(
            [
                *_flags(roots),
                "plan",
                "--strategy-instance-id",
                ARMING_SID,
                "--now-ms",
                str(ARMED_AT_MS),
            ]
        )
        == 0
    )

    plan = _last_object(capsys)
    assert plan["envelope_sha256"] == TEST_ENVELOPE_VALUES.sha
    assert plan["envelope_values"] == TEST_ENVELOPE_VALUES.to_mapping()


def test_plan_and_apply_refuse_while_a_different_revision_is_staged(
    profiles: BrokerConfigurationService,
    roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Owner decisions D3 and D5: arm what is running, and name both revisions."""
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    effective_id = _live_profile(profiles, display_name="Live — one", envelope=TEST_ENVELOPE_VALUES)
    _make_effective(profiles, effective_id, 1)
    staged_id = _live_profile(profiles, display_name="Live — two", envelope=OTHER_ENVELOPE)
    profiles.stage_selection(
        profile_id=staged_id,
        revision=1,
        expected_selection_generation=profiles.selection().selection_generation,
    )

    assert (
        main(
            [
                *_flags(roots),
                "plan",
                "--strategy-instance-id",
                ARMING_SID,
                "--now-ms",
                str(ARMED_AT_MS),
            ]
        )
        == 2
    )

    refusal = _last_object(capsys)
    assert refusal["error"] == LIVE_ARMING_REVISION_STAGED
    assert f"{staged_id}@1 is staged" in refusal["detail"]
    assert f"{effective_id}@1 is effective" in refusal["detail"]

    plan_file = tmp_path / "plan.json"
    plan_file.write_text("{}", encoding="utf-8")
    assert (
        main(
            [
                *_flags(roots),
                "apply",
                "--plan-file",
                str(plan_file),
                "--confirmation-token",
                "irrelevant",
            ]
        )
        == 2
    )
    assert _last_object(capsys)["error"] == LIVE_ARMING_REVISION_STAGED
    assert LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT).records() == ()


def test_status_and_disarm_still_answer_while_a_different_revision_is_staged(
    profiles: BrokerConfigurationService,
    roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The closed direction and the read are never gated by a staged change.

    An operator who has just staged a limit change is precisely the operator who
    may need to revoke a permission. Gating ``disarm`` on the selection would
    make a half-finished configuration edit block the only command that takes
    authority *away*.
    """
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    effective_id = _live_profile(profiles, display_name="Live — one", envelope=TEST_ENVELOPE_VALUES)
    _make_effective(profiles, effective_id, 1)
    _arm_from_the_effective_revision(roots, capsys)
    staged_id = _live_profile(profiles, display_name="Live — two", envelope=OTHER_ENVELOPE)
    profiles.stage_selection(
        profile_id=staged_id,
        revision=1,
        expected_selection_generation=profiles.selection().selection_generation,
    )

    assert main([*_flags(roots), "status", "--now-ms", str(ARMED_AT_MS)]) == 0
    status = _last_object(capsys)
    assert status["live_account_id"] == LIVE_ACCT
    assert status["armed_instance_count"] == 1

    assert (
        main(
            [
                *_flags(roots),
                "disarm",
                "--strategy-instance-id",
                ARMING_SID,
                "--now-ms",
                str(ARMED_AT_MS),
            ]
        )
        == 0
    )
    assert _last_object(capsys)["kind"] == "disarmed"


def test_staging_the_effective_revision_again_is_not_a_divergence(
    profiles: BrokerConfigurationService,
    roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``acknowledge_effective`` never clears ``staged_*``, so equality must pass.

    After an ordinary stage → Apply → restart cycle the staged and effective
    revisions are the same row. Refusing on "something is staged" rather than on
    "something *different* is staged" would make every armed installation
    unarmable one restart later.
    """
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    profile_id = _live_profile(profiles, display_name="Live — primary", envelope=TEST_ENVELOPE_VALUES)
    _make_effective(profiles, profile_id, 1)
    assert profiles.selection().staged_profile_id == profile_id

    assert (
        main(
            [
                *_flags(roots),
                "plan",
                "--strategy-instance-id",
                ARMING_SID,
                "--now-ms",
                str(ARMED_AT_MS),
            ]
        )
        == 0
    )
    assert _last_object(capsys)["envelope_sha256"] == TEST_ENVELOPE_VALUES.sha


def test_plan_refuses_when_the_installation_has_applied_nothing(
    profiles: BrokerConfigurationService,
    roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A configured installation with no effective revision arms nothing."""
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    _live_profile(profiles, display_name="Live — primary", envelope=TEST_ENVELOPE_VALUES)

    assert (
        main(
            [
                *_flags(roots),
                "plan",
                "--strategy-instance-id",
                ARMING_SID,
                "--now-ms",
                str(ARMED_AT_MS),
            ]
        )
        == 2
    )

    refusal = _last_object(capsys)
    assert refusal["error"] == "LIVE_ENVELOPE_MISSING"
    assert BROKER_UNCONFIGURED in refusal["detail"]
    assert LiveArmingLedger(artifacts_root, live_account_id=LIVE_ACCT).records() == ()


# ---- the before→after diff -------------------------------------------------


def test_the_first_plan_on_an_account_reports_sealing_all_six_values(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)

    assert (
        main(
            [
                *_flags(roots),
                "plan",
                "--strategy-instance-id",
                ARMING_SID,
                "--now-ms",
                str(ARMED_AT_MS),
            ],
            settings=SETTINGS,
        )
        == 0
    )

    change = _last_object(capsys)["envelope_change"]
    assert change["changed"] is True
    assert change["sealed_by"] is None
    assert change["summary"].startswith("FIRST SEAL:")
    assert [entry["field"] for entry in change["changes"]] == list(
        TEST_ENVELOPE_VALUES.to_mapping()
    )
    assert all(entry["before"] is None for entry in change["changes"])


def test_a_plan_that_changes_nothing_says_so(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    _arm(roots, capsys)

    assert (
        main(
            [
                *_flags(roots),
                "plan",
                "--strategy-instance-id",
                ARMING_SID,
                "--now-ms",
                str(ARMED_AT_MS),
            ],
            settings=SETTINGS,
        )
        == 0
    )

    change = _last_object(capsys)["envelope_change"]
    assert change["changed"] is False
    assert change["changes"] == []
    assert change["summary"].startswith("NO CHANGE:")
    assert change["sealed_by"]["strategy_instance_id"] == ARMING_SID
    assert change["sealed_by"]["envelope_sha256"] == TEST_ENVELOPE_VALUES.sha


def test_a_plan_that_moves_a_limit_shows_every_changed_value_before_after(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator's last look before a real-money limit changes."""
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    _arm(roots, capsys)

    assert (
        main(
            [
                *_flags(roots),
                "plan",
                "--strategy-instance-id",
                ARMING_SID,
                "--now-ms",
                str(ARMED_AT_MS),
            ],
            settings=live_settings(
                live_loss_usd=OTHER_ENVELOPE.loss_usd,
                live_arming_max_sessions=OTHER_ENVELOPE.arming_max_sessions,
                live_xh_exit_bps=OTHER_ENVELOPE.xh_exit_bps,
            ),
        )
        == 0
    )

    change = _last_object(capsys)["envelope_change"]
    assert change["changed"] is True
    assert change["changes"] == [
        {"field": "loss_usd", "before": TEST_ENVELOPE_VALUES.loss_usd, "after": 7500.0},
        {"field": "arming_max_sessions", "before": TEST_ENVELOPE_VALUES.arming_max_sessions, "after": 5},
        {"field": "xh_exit_bps", "before": TEST_ENVELOPE_VALUES.xh_exit_bps, "after": 25.0},
    ]
    assert change["summary"] == (
        "CHANGED: 3 of the 6 live envelope values differ from the envelope sealed on this "
        f"account by {ARMING_SID}: loss_usd 5000.0 -> 7500.0; arming_max_sessions 20 -> 5; "
        "xh_exit_bps 10.0 -> 25.0."
    )


def test_a_plan_saved_from_stdout_still_reads_back_as_a_plan(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The diff is a report *about* the plan, so ``apply`` must strip it.

    An operator who redirects ``plan``'s stdout to a file and applies that file
    is the documented path; a new reported key that ``_read_plan`` did not pop
    would turn it into "arming plan file is not an arming plan".
    """
    artifacts_root, live_state_root = roots
    arming_ready(artifacts_root, live_state_root)
    assert (
        main(
            [
                *_flags(roots),
                "plan",
                "--strategy-instance-id",
                ARMING_SID,
                "--now-ms",
                str(ARMED_AT_MS),
            ],
            settings=SETTINGS,
        )
        == 0
    )
    printed = _last_object(capsys)
    assert "envelope_change" in printed
    plan_file = tmp_path / "plan-from-stdout.json"
    plan_file.write_text(json.dumps(printed), encoding="utf-8")

    assert (
        main(
            [
                *_flags(roots),
                "apply",
                "--plan-file",
                str(plan_file),
                "--confirmation-token",
                printed["confirmation_token"],
                "--now-ms",
                str(ARMED_AT_MS),
            ],
            settings=SETTINGS,
        )
        == 0
    )
    assert _last_object(capsys)["envelope_sha256"] == TEST_ENVELOPE_VALUES.sha


def _arm(
    roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    *,
    settings: AlpacaSettings | None = SETTINGS,
) -> None:
    """One real plan → apply pair, so an envelope is sealed on the account.

    ``settings=None`` is not "no settings": it is the CLI's own default, which
    resolves the effective revision out of the profiles database.
    """
    artifacts_root, _live_state_root = roots
    plan_file = artifacts_root / "seed-plan.json"
    assert (
        main(
            [
                *_flags(roots),
                "plan",
                "--strategy-instance-id",
                ARMING_SID,
                "--plan-out",
                str(plan_file),
                "--now-ms",
                str(ARMED_AT_MS),
            ],
            settings=settings,
        )
        == 0
    )
    token = _last_object(capsys)["confirmation_token"]
    assert (
        main(
            [
                *_flags(roots),
                "apply",
                "--plan-file",
                str(plan_file),
                "--confirmation-token",
                token,
                "--now-ms",
                str(ARMED_AT_MS),
            ],
            settings=settings,
        )
        == 0
    )
    capsys.readouterr()


def _arm_from_the_effective_revision(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """The same pair with nothing injected, so the profiles database decides."""
    _arm(roots, capsys, settings=None)
