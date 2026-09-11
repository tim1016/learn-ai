"""Startup locks and acknowledgement keep unselected writers out of custody."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.broker.alpaca.active_binding import reset_active_alpaca_binding_for_testing
from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.broker.alpaca.clerk.live_arming import LiveArmingInvalid
from app.broker.alpaca.profile import resolve_runtime_context
from app.broker_configuration.binding_decision import BindingCandidate, BindingIntent
from app.broker_configuration.service import BrokerConfigurationService
from app.broker_configuration.worker_binding import BoundWorker
from app.broker_configuration.worker_lifecycle import (
    acknowledge_runtime_binding,
    installation_worker,
)
from tests.broker.alpaca.profile.conftest import make_environment
from tests.broker_configuration.conftest import paper_profile


@pytest.fixture(autouse=True)
def isolated_binding() -> Iterator[None]:
    reset_active_alpaca_binding_for_testing()
    yield
    reset_active_alpaca_binding_for_testing()


def _bound(profile_id: str, generation: int) -> BoundWorker:
    return BoundWorker(
        context=resolve_runtime_context(
            endpoint_mode="paper", credential_slot="default",
            profile_id=profile_id, revision=1, account_pin="PA-TEST",
            environment=make_environment(api_key_id="fake-key", api_secret_key="fake-secret"),
        ),
        candidate=BindingCandidate(
            profile_id=profile_id, revision=1, intent=BindingIntent.APPLY,
            selection_generation=generation, previous_profile_id=None,
            previous_revision=None, previous_account_id=None,
        ),
    )


def test_installation_lock_excludes_a_second_process_and_releases_after_crash(tmp_path: Path) -> None:
    script = (
        "from pathlib import Path\n"
        "import sys\n"
        "from app.broker_configuration.worker_lifecycle import installation_worker\n"
        "with installation_worker(clerk_dir=Path(sys.argv[1])) as refusal:\n"
        "    sys.stdout.write('owned\\n' if refusal is None else 'refused\\n')\n"
        "    sys.stdout.flush()\n"
        "    sys.stdin.read()\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
        env={"POLYGON_API_KEY": "fake-lifecycle-test", "DATA_PLANE_CONTROL_SECRET": ""},
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "owned"
        with installation_worker(clerk_dir=tmp_path) as refusal:
            assert refusal is not None
            assert "Another worker" in refusal.message
        process.kill()
        process.communicate(timeout=5)
        with installation_worker(clerk_dir=tmp_path) as refusal:
            assert refusal is None
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


async def test_refused_acknowledgement_closes_custody_before_exposing_a_writer(
    service: BrokerConfigurationService,
) -> None:
    first = paper_profile(service, display_name="First")
    second = paper_profile(service, display_name="Second")
    staged = service.stage_selection(
        profile_id=first.profile.profile_id, revision=1, expected_selection_generation=0
    )
    bound = _bound(first.profile.profile_id, staged.selection_generation)
    service.stage_selection(
        profile_id=second.profile.profile_id, revision=1,
        expected_selection_generation=staged.selection_generation,
    )
    runtime = ActiveClerkRuntime(authority_kind="sqlite", clerk=object(), account_id="PA-TEST")
    runtime.close = AsyncMock()

    result = await acknowledge_runtime_binding(bound=bound, runtime=runtime, service_factory=lambda: service)

    runtime.close.assert_awaited_once()
    assert result.clerk is None
    assert result.startup_failure is not None
    assert result.startup_failure.reason_code == "SELECTION_GENERATION_CONFLICT"
    assert service.selection().effective_profile_id is None


async def test_shadow_acknowledges_the_broker_account_not_its_custody_namespace(
    service: BrokerConfigurationService,
) -> None:
    profile = paper_profile(service)
    staged = service.stage_selection(
        profile_id=profile.profile.profile_id, revision=1, expected_selection_generation=0
    )
    bound = _bound(profile.profile.profile_id, staged.selection_generation)
    runtime = ActiveClerkRuntime(authority_kind="shadow", clerk=object(), account_id="shadow:PA-TEST")

    result = await acknowledge_runtime_binding(bound=bound, runtime=runtime, service_factory=lambda: service)

    assert result is runtime
    assert service.selection().effective_account_id == "PA-TEST"


async def test_unreadable_arming_evidence_during_acknowledgement_closes_custody(
    service: BrokerConfigurationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = paper_profile(service)
    bound = _bound(profile.profile.profile_id, 0)
    runtime = ActiveClerkRuntime(authority_kind="sqlite", clerk=object(), account_id="PA-TEST")
    runtime.close = AsyncMock()

    def unreadable(**_kwargs: object) -> None:
        raise LiveArmingInvalid("malformed arming ledger")

    monkeypatch.setattr(service, "acknowledge_effective", unreadable)
    result = await acknowledge_runtime_binding(bound=bound, runtime=runtime, service_factory=lambda: service)

    runtime.close.assert_awaited_once()
    assert result.clerk is None
    assert result.startup_failure.reason_code == "LIVE_ARMING_LEDGER_INVALID"


async def test_failed_startup_closes_custody_before_releasing_installation_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import main
    from app.broker.alpaca.clerk.active_authority import (
        get_active_clerk_runtime,
        set_active_clerk_runtime,
    )
    from app.broker_configuration import worker_lifecycle

    runtime = ActiveClerkRuntime(authority_kind="sqlite", clerk=object(), account_id="PA-TEST")

    async def close_while_owned() -> None:
        with installation_worker(clerk_dir=tmp_path) as refusal:
            assert refusal is not None

    runtime.close = AsyncMock(side_effect=close_while_owned)

    @asynccontextmanager
    async def fail_after_custody(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
        set_active_clerk_runtime(runtime)
        raise RuntimeError("later startup step failed")
        yield  # pragma: no cover

    monkeypatch.setattr(worker_lifecycle, "resolve_clerk_dir", lambda: tmp_path)
    monkeypatch.setattr(main, "_service_lifespan", fail_after_custody)
    with pytest.raises(RuntimeError, match="later startup"):
        async with main.lifespan(main.app):
            pytest.fail("startup must have failed")

    runtime.close.assert_awaited_once()
    assert get_active_clerk_runtime() is None
    with installation_worker(clerk_dir=tmp_path) as refusal:
        assert refusal is None
