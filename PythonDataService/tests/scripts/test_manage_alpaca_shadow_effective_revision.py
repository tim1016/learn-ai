"""The shadow CLI's two unstated values, resolved from the effective revision.

``--artifacts-root`` and ``--required-sessions`` are the only two inputs this
CLI will take from configuration rather than from the command line. Package D
moved both from the process environment to the installation's effective profile
revision (ADR 0060), and this file pins the three things that must stay true of
that move: the value comes from the effective revision, a binding that will not
resolve refuses the command instead of falling back, and an invocation that
states both values opens no profiles database at all.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import scripts.manage_alpaca_shadow as shadow_cli_module
from app.broker.alpaca.active_binding import BROKER_UNCONFIGURED
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues
from app.broker.alpaca.config import AlpacaSettings, reset_alpaca_settings_for_testing
from app.broker_configuration import runtime as broker_configuration_runtime
from app.broker_configuration.envelope import ValidatedLiveEnvelope
from app.broker_configuration.runtime import (
    build_service,
    reset_broker_configuration_service_for_testing,
)
from app.broker_configuration.service import BrokerConfigurationService
from app.services.alpaca_shadow_reconciliation import (
    ShadowGateEvaluation,
    ShadowSessionVerdict,
    TwinDayReconciliation,
)
from scripts.manage_alpaca_shadow import main
from tests.broker.alpaca.clerk.live_arming_fixtures import record_sealed_binding
from tests.broker.alpaca.clerk.live_envelope_fixtures import LIVE_ACCT, TEST_ENVELOPE_VALUES

SID, TWIN = "shadow-sid", "twin-sid"
TWIN_ACCOUNT = "PA-TWIN-CFG"

# Three, so a pass cannot be a coincidence with the fixture envelope's one or
# with any default a future refactor might introduce.
CONFIGURED_SHADOW_SESSIONS = 3
CONFIGURED_ENVELOPE = LiveEnvelopeValues(
    loss_fraction=TEST_ENVELOPE_VALUES.loss_fraction,
    loss_usd=TEST_ENVELOPE_VALUES.loss_usd,
    shadow_sessions=CONFIGURED_SHADOW_SESSIONS,
    arming_max_sessions=TEST_ENVELOPE_VALUES.arming_max_sessions,
    xh_entry_bps=TEST_ENVELOPE_VALUES.xh_entry_bps,
    xh_exit_bps=TEST_ENVELOPE_VALUES.xh_exit_bps,
)


def _last_object(capsys: pytest.CaptureFixture[str]) -> dict:
    return json.loads(capsys.readouterr().out.splitlines()[-1])


@pytest.fixture()
def clerk_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An Alpaca environment and a Clerk root that are entirely this test's."""
    root = tmp_path / "clerk"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALPACA_API_KEY_ID", "key")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "secret")
    monkeypatch.setenv("ALPACA_CREDENTIAL_LIVE_KEY_ID", "live-key")
    monkeypatch.setenv("ALPACA_CREDENTIAL_LIVE_SECRET_KEY", "live-secret")
    # No ``ALPACA_MODE``: these tests make a revision effective, i.e. the
    # installation has cut over, and package F refuses to resolve a binding
    # while a retired variable is still set. Deleting it is what a real
    # cut-over deployment does, and ``AlpacaSettings.mode`` defaults to
    # "paper" regardless, so nothing else here changes.
    monkeypatch.delenv("ALPACA_MODE", raising=False)
    monkeypatch.setenv("ALPACA_CLERK_DIR", str(root))
    # ``tests/conftest.py`` pins the profiles database inside tmp_path by
    # patching this resolver; a test that needs its own location patches it
    # again, later patch winning.
    monkeypatch.setattr(broker_configuration_runtime, "resolve_clerk_dir", lambda: root)
    reset_alpaca_settings_for_testing()
    reset_broker_configuration_service_for_testing()
    yield root
    reset_alpaca_settings_for_testing()
    reset_broker_configuration_service_for_testing()


@pytest.fixture()
def profiles(clerk_dir: Path) -> Iterator[BrokerConfigurationService]:
    service = build_service(clerk_dir=clerk_dir)
    yield service
    service.close()


def _live_profile(service: BrokerConfigurationService, *, envelope: LiveEnvelopeValues) -> str:
    created = service.create_profile(
        display_name="Live — primary",
        credential_slot="live",
        endpoint_mode="live",
        live_envelope=ValidatedLiveEnvelope.from_mapping(envelope.to_mapping()),
    )
    return created.profile.profile_id


def _make_effective(service: BrokerConfigurationService, profile_id: str) -> None:
    service.stage_selection(
        profile_id=profile_id,
        revision=1,
        expected_selection_generation=service.selection().selection_generation,
    )
    service.acknowledge_effective(
        profile_id=profile_id,
        revision=1,
        account_id=LIVE_ACCT,
        expected_selection_generation=service.selection().selection_generation,
    )


def _judged(required: int) -> ShadowGateEvaluation:
    """One judged report with nothing counted, so ``sessions`` only reports."""
    return ShadowGateEvaluation(
        live_account_id=LIVE_ACCT,
        strategy_instance_id=SID,
        twin_account_id=TWIN_ACCOUNT,
        twin_strategy_instance_id=TWIN,
        configured_signal_hash="a" * 64,
        required_sessions=required,
        sessions=(
            ShadowSessionVerdict(
                session_open_ms=1_000,
                state="skipped",
                detail="",
                shadow_run_id=None,
                reconciliation=TwinDayReconciliation(
                    1_000, SID, TWIN, (), (), (), None, None, Decimal("0.01")
                ),
            ),
        ),
    )


def _sessions_argv(clerk_dir: Path, *, artifacts_root: bool = True) -> list[str]:
    argv = ["--live-account-id", LIVE_ACCT]
    if artifacts_root:
        argv += ["--artifacts-root", str(clerk_dir)]
    return [
        *argv,
        "--live-state-root",
        str(clerk_dir / "live"),
        "sessions",
        "--strategy-instance-id",
        SID,
        "--twin-account-id",
        TWIN_ACCOUNT,
        "--twin-strategy-instance-id",
        TWIN,
    ]


def _never_called(**_kwargs: Any) -> ShadowGateEvaluation:
    raise AssertionError("the gate must not be judged")


def test_the_required_session_count_comes_from_the_effective_revision(
    profiles: BrokerConfigurationService,
    clerk_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _make_effective(profiles, _live_profile(profiles, envelope=CONFIGURED_ENVELOPE))
    record_sealed_binding(clerk_dir / "live", strategy_instance_id=SID)
    record_sealed_binding(clerk_dir / "live", strategy_instance_id=TWIN)
    asked: list[int] = []

    def _capture(**kwargs: Any) -> ShadowGateEvaluation:
        asked.append(kwargs["required_sessions"])
        return _judged(kwargs["required_sessions"])

    assert main(_sessions_argv(clerk_dir), evaluate=_capture) == 0

    assert asked == [CONFIGURED_SHADOW_SESSIONS]
    assert _last_object(capsys)["required"] == CONFIGURED_SHADOW_SESSIONS


def test_an_unresolvable_binding_refuses_rather_than_falling_back(
    profiles: BrokerConfigurationService,
    clerk_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A configured installation with nothing applied has no settings to read.

    ``--artifacts-root`` is omitted here on purpose: that is the read taken
    before dispatch, and it must refuse in this CLI's own vocabulary rather than
    resolve a default Clerk root or raise a traceback.
    """
    _live_profile(profiles, envelope=CONFIGURED_ENVELOPE)

    assert main(_sessions_argv(clerk_dir, artifacts_root=False), evaluate=_never_called) == 1

    assert BROKER_UNCONFIGURED in _last_object(capsys)["error"]


def test_stating_both_values_never_resolves_a_binding(
    clerk_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The two reads stay conditional, exactly as they were before the move."""
    record_sealed_binding(clerk_dir / "live", strategy_instance_id=SID)
    record_sealed_binding(clerk_dir / "live", strategy_instance_id=TWIN)

    def _explode() -> AlpacaSettings:
        raise AssertionError("a fully-stated invocation must resolve no broker binding")

    monkeypatch.setattr(shadow_cli_module, "effective_alpaca_settings", _explode)

    assert (
        main(
            [*_sessions_argv(clerk_dir), "--required-sessions", "2"],
            evaluate=lambda **_kwargs: _judged(2),
        )
        == 0
    )
    assert _last_object(capsys)["required"] == 2


def test_injected_settings_still_bypass_the_profiles_database(
    clerk_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The seam the arming CLI already had, in the same shape, for this one too."""
    record_sealed_binding(clerk_dir / "live", strategy_instance_id=SID)
    record_sealed_binding(clerk_dir / "live", strategy_instance_id=TWIN)

    def _explode() -> AlpacaSettings:
        raise AssertionError("injected settings must resolve no broker binding")

    monkeypatch.setattr(shadow_cli_module, "effective_alpaca_settings", _explode)

    assert (
        main(
            _sessions_argv(clerk_dir),
            evaluate=lambda **kwargs: _judged(kwargs["required_sessions"]),
            settings=AlpacaSettings(
                api_key_id="k",
                api_secret_key="s",
                mode="live",
                live_loss_fraction=CONFIGURED_ENVELOPE.loss_fraction,
                live_loss_usd=CONFIGURED_ENVELOPE.loss_usd,
                live_shadow_sessions=CONFIGURED_SHADOW_SESSIONS,
                live_arming_max_sessions=CONFIGURED_ENVELOPE.arming_max_sessions,
                live_xh_entry_bps=CONFIGURED_ENVELOPE.xh_entry_bps,
                live_xh_exit_bps=CONFIGURED_ENVELOPE.xh_exit_bps,
            ),
        )
        == 0
    )
    assert _last_object(capsys)["required"] == CONFIGURED_SHADOW_SESSIONS
