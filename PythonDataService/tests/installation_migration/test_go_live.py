"""``migrate-installation go-live`` (#2269).

Pinned here: after import every lane refuses a bot start; go-live proves bars
on every lane before it asks for the operator's "old machine is off", and
releases nothing unless both hold — the bar check alone is not enough, and
the confirmation alone is not enough. Bots stay stopped after go-live.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker_configuration.runtime import CLERK_DIR_ENV_VAR
from app.installation_migration.errors import MigrationRefused
from app.installation_migration.export import ExportRequest, run_export
from app.installation_migration.golive import CONFIRMATION_PROMPT, GoLiveRequest, run_go_live
from app.installation_migration.importer import ImportRequest, run_import
from app.installation_migration.lanes import Lane
from app.services.bot_runner import (
    BotRunnerError,
    BotTaskRegistry,
    RunAdmissionRefusedError,
    go_live_start_gate,
)
from app.services.bot_runner_errors import LANE_GO_LIVE_PENDING
from app.services.go_live_hold import (
    GO_LIVE_HOLD_MARKER,
    GoLiveHoldMarker,
    go_live_marker_bytes,
    read_go_live_hold,
)
from app.services.lane_go_live import lane_go_live_hold
from tests.installation_migration._support import (
    LIVE_VOLUME,
    PAPER_VOLUME,
    T0,
    FakeGit,
    FakeGoLiveLanes,
    build_empty_destination,
    build_installation,
)

_CONFIRMED = "the old machine is off"
_REQUEST = GoLiveRequest(operator="inkant", change_ref="go-live-2026-09-23")


class _Operator:
    """Types a scripted line when asked, and remembers whether it was asked."""

    def __init__(self, typed: str = _CONFIRMED) -> None:
        self.typed = typed
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.typed


def _held_lanes(tmp_path: Path) -> FakeGoLiveLanes:
    roots: dict[str, Path] = {}
    for clerk_id in ("clrk_live", "clrk_paper"):
        root = tmp_path / clerk_id
        root.mkdir()
        marker = GoLiveHoldMarker(
            kind="learn-ai-go-live-hold",
            schema_version=1,
            written_at_ms=T0,
            volume=clerk_id,
            source_commit="a" * 40,
            registry_id="reg_1",
        )
        (root / GO_LIVE_HOLD_MARKER).write_bytes(go_live_marker_bytes(marker))
        roots[clerk_id] = root
    return FakeGoLiveLanes(
        lanes=[
            Lane("clrk_live", "alpaca", "provisioned", "Live"),
            Lane("clrk_paper", "alpaca", "provisioned", "Paper"),
        ],
        roots=roots,
    )


def _go_live(lanes: FakeGoLiveLanes, operator: _Operator) -> list[dict]:
    steps: list[dict] = []
    run_go_live(_REQUEST, lanes=lanes, confirm=operator, emit=steps.append)
    return steps


def _held(lanes: FakeGoLiveLanes) -> dict[str, bool]:
    return {clerk_id: read_go_live_hold(root).held for clerk_id, root in lanes.roots.items()}


def test_go_live_proves_bars_everywhere_then_confirms_then_releases_every_lane(
    tmp_path: Path,
) -> None:
    lanes = _held_lanes(tmp_path)
    operator = _Operator()

    steps = _go_live(lanes, operator)

    assert lanes.calls == [
        ("bar-check", "clrk_live"),
        ("bar-check", "clrk_paper"),
        ("release", "clrk_live"),
        ("release", "clrk_paper"),
    ]
    assert operator.prompts == [CONFIRMATION_PROMPT]
    assert [step["step"] for step in steps] == [
        "lanes",
        "bars-proven",
        "bars-proven",
        "old-machine-off-confirmed",
        "lane-released",
        "lane-released",
        "complete",
    ]
    assert _held(lanes) == {"clrk_live": False, "clrk_paper": False}
    assert steps[-1]["released"] == ["clrk_live", "clrk_paper"]
    assert "No bot was started" in steps[-1]["next"]


def test_completion_claims_only_the_lanes_the_coordinator_listed(tmp_path: Path) -> None:
    """Go-live cannot see a held volume the coordinator does not list, so its
    completion names what it released instead of calling every lane live."""
    lanes = _held_lanes(tmp_path)
    lanes.lanes = [lane for lane in lanes.lanes if lane.clerk_id == "clrk_live"]

    steps = _go_live(lanes, _Operator())

    complete = steps[-1]
    assert complete["released"] == ["clrk_live"]
    assert "Every lane the coordinator listed is released: clrk_live." in complete["next"]
    assert "does not list was not released and still holds its bots" in complete["next"]
    assert "Every lane is live" not in complete["next"]
    assert _held(lanes) == {"clrk_live": False, "clrk_paper": True}


@pytest.mark.parametrize("typed", ["", "yes", "the old machine is on", "THE OLD MACHINE"])
def test_the_bar_check_alone_is_not_enough(tmp_path: Path, typed: str) -> None:
    lanes = _held_lanes(tmp_path)

    with pytest.raises(MigrationRefused) as refused:
        _go_live(lanes, _Operator(typed))

    assert refused.value.reason == "old_machine_off_not_confirmed"
    assert [call for call in lanes.calls if call[0] == "release"] == []
    assert _held(lanes) == {"clrk_live": True, "clrk_paper": True}


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        ("lane_ibkr_bar_check_failed", "503 ibkr_gateway_unreachable: no IB Gateway client"),
        ("lane_ibkr_bar_check_failed", "503 ibkr_no_bars: no bars came back"),
        ("coordinator_unreachable", "could not reach the coordinator"),
    ],
    ids=["gateway_unreachable", "zero_bars", "coordinator_down"],
)
def test_the_confirmation_alone_is_not_enough(tmp_path: Path, reason: str, message: str) -> None:
    """A lane that cannot prove a bar stops go-live before the operator is
    even asked — and a lane that already passed is not released either."""
    lanes = _held_lanes(tmp_path)
    lanes.bars["clrk_paper"] = MigrationRefused(reason, message, details={"clerk_id": "clrk_paper"})
    operator = _Operator()

    with pytest.raises(MigrationRefused) as refused:
        _go_live(lanes, operator)

    assert refused.value.reason == reason
    assert "nothing was released" in refused.value.message
    assert refused.value.details["bars_proven_on"] == ["clrk_live"]
    assert operator.prompts == []
    assert [call for call in lanes.calls if call[0] == "release"] == []
    assert _held(lanes) == {"clrk_live": True, "clrk_paper": True}


def test_a_release_that_fails_names_what_was_released_and_what_is_still_held(
    tmp_path: Path,
) -> None:
    lanes = _held_lanes(tmp_path)
    lanes.fail_release.add("clrk_paper")

    with pytest.raises(MigrationRefused) as refused:
        _go_live(lanes, _Operator())

    assert refused.value.details["released"] == ["clrk_live"]
    assert refused.value.details["still_held"] == ["clrk_paper"]
    assert "Re-run go-live" in refused.value.message
    assert _held(lanes) == {"clrk_live": False, "clrk_paper": True}


def test_rerunning_go_live_after_a_partial_release_finishes_it(tmp_path: Path) -> None:
    lanes = _held_lanes(tmp_path)
    lanes.fail_release.add("clrk_paper")
    with pytest.raises(MigrationRefused):
        _go_live(lanes, _Operator())
    lanes.fail_release.clear()

    steps = _go_live(lanes, _Operator())

    assert _held(lanes) == {"clrk_live": False, "clrk_paper": False}
    released = {step["clerk_id"]: step["was_held"] for step in steps if step["step"] == "lane-released"}
    assert released == {"clrk_live": False, "clrk_paper": True}


def test_no_live_lane_refuses(tmp_path: Path) -> None:
    lanes = _held_lanes(tmp_path)
    lanes.lanes = [Lane("clrk_old", "alpaca", "retired", "Old")]

    with pytest.raises(MigrationRefused) as refused:
        _go_live(lanes, _Operator())

    assert refused.value.reason == "no_lanes"


async def test_after_import_a_bot_start_refuses_until_go_live_and_nothing_starts_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole seam, end to end: export, import, then each restored lane's
    own start gate — ``lane_go_live_hold``, exactly as ``app.main`` wires it,
    with the clerk directory at the restored volume's root and the runner's
    artifacts root elsewhere — refuses until go-live has released it;
    go-live itself starts nothing."""
    source = build_installation(tmp_path)
    bundle = tmp_path / "bundle.tar"
    run_export(
        ExportRequest(
            repo_root=source.repo_root, bundle_path=bundle, operator="inkant", change_ref="m"
        ),
        lanes=source.lanes,
        podman=source.podman,
        git=FakeGit(),
        emit=lambda _step: None,
    )
    repo_root, podman = build_empty_destination(tmp_path)
    run_import(
        ImportRequest(repo_root=repo_root, bundle_path=bundle),
        podman=podman,
        git=FakeGit(),
        emit=lambda _step: None,
    )
    roots = {
        source.live_clerk_id: podman.volume_dir(LIVE_VOLUME),
        source.paper_clerk_id: podman.volume_dir(PAPER_VOLUME),
    }
    registries = {
        clerk_id: BotTaskRegistry(
            tmp_path / "artifacts" / clerk_id,
            feed_resolver=lambda: None,
            boot_recovery_required=False,
            lane_start_gates=(go_live_start_gate(lane_go_live_hold),),
        )
        for clerk_id in roots
    }
    for clerk_id, registry in registries.items():
        monkeypatch.setenv(CLERK_DIR_ENV_VAR, str(roots[clerk_id]))
        with pytest.raises(RunAdmissionRefusedError) as refused:
            await registry.deploy(broker="alpaca", strategy_instance_id="ema-1", symbol="SPY")
        assert refused.value.reason_code == LANE_GO_LIVE_PENDING

    lanes = FakeGoLiveLanes(lanes=source.lanes.lanes, roots=roots)
    _go_live(lanes, _Operator())

    for clerk_id, registry in registries.items():
        monkeypatch.setenv(CLERK_DIR_ENV_VAR, str(roots[clerk_id]))
        # Go-live started nothing; released, the hold no longer answers a
        # start — a later admission gate (no clerk in this test) does.
        assert registry.any_running() is False
        with pytest.raises(BotRunnerError) as later:
            await registry.deploy(broker="alpaca", strategy_instance_id="ema-1", symbol="SPY")
        assert later.value.reason_code is None
        assert "go-live" not in str(later.value)
