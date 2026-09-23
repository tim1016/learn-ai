"""The lane's installation-migration go-live routes (#2269).

``GET /api/brokers/alpaca/lane/ibkr-bar-check`` proves IB Gateway returns a
real historical bar to this lane; ``POST
/api/brokers/alpaca/lane/go-live/release`` removes the lane's go-live hold,
and only with the operator's confirmation words **and** a fresh passing bar
check. Both are the agent paths the coordinator forwards the catalog's
``lane_ibkr_bar_check`` and ``lane_go_live_release`` operations to.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.broker_configuration.runtime import CLERK_DIR_ENV_VAR
from app.config import settings
from app.main import app
from app.services import lane_go_live
from app.services.bot_runner import set_bot_task_registry
from app.services.go_live_hold import (
    GO_LIVE_HOLD_MARKER,
    GO_LIVE_RECEIPTS_DIRECTORY,
    GoLiveHoldMarker,
    go_live_marker_bytes,
    read_go_live_hold,
)
from app.services.lane_go_live import forget_bar_checks
from tests._helpers.bot_runner.custody import _registry
from tests._helpers.bot_runner.doubles import _FakeFeed

_CHECK_PATH = "/api/brokers/alpaca/lane/ibkr-bar-check"
_RELEASE_PATH = "/api/brokers/alpaca/lane/go-live/release"
_BODY = {
    "operator": "inkant",
    "change_ref": "go-live-2026-09-23",
    "old_machine_off_confirmation": "the old machine is off",
}
_T0 = 1_789_100_000_000


class _Client:
    connection_lost = False

    def is_connected(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _clean_globals() -> Iterator[None]:
    set_bot_task_registry(None)
    forget_bar_checks()
    yield
    set_bot_task_registry(None)
    forget_bar_checks()


def _gateway(monkeypatch: pytest.MonkeyPatch, bars: list[SimpleNamespace]) -> None:
    async def fetch(_client: object, _symbol: str, **_kwargs: object) -> list[SimpleNamespace]:
        return bars

    monkeypatch.setattr(lane_go_live, "get_client", _Client)
    monkeypatch.setattr(lane_go_live, "fetch_historical_minute_bars", fetch)


def _bars() -> list[SimpleNamespace]:
    return [SimpleNamespace(start_ms=_T0 - 60_000, end_ms=_T0)]


def _volume(tmp_path: Path) -> Path:
    """The clerk volume's mount point — deliberately not the runner's artifacts root."""
    return tmp_path / "clerk_volume"


@pytest.fixture(autouse=True)
def _clerk_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _volume(tmp_path).mkdir()
    monkeypatch.setenv(CLERK_DIR_ENV_VAR, str(_volume(tmp_path)))


def _held_lane(tmp_path: Path) -> None:
    """A hold at the clerk volume root, and a bot runner whose artifacts root
    is elsewhere — as in the combined role, where it is ``/app/artifacts``."""
    marker = GoLiveHoldMarker(
        kind="learn-ai-go-live-hold",
        schema_version=1,
        written_at_ms=_T0,
        volume="learn-ai-alpaca-clerk-data",
        source_commit="a" * 40,
        registry_id="reg_1",
    )
    (_volume(tmp_path) / GO_LIVE_HOLD_MARKER).write_bytes(go_live_marker_bytes(marker))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    set_bot_task_registry(_registry(artifacts, _FakeFeed([], mode="hold")))


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_the_bar_check_answers_the_bars_it_proved(monkeypatch: pytest.MonkeyPatch) -> None:
    _gateway(monkeypatch, _bars())

    async with _client() as client:
        response = await client.get(_CHECK_PATH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["symbol"], body["bar_count"]) == ("SPY", 1)
    assert body["last_bar_end_ms"] == _T0


async def test_a_gateway_with_no_bars_is_a_503_never_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _gateway(monkeypatch, [])

    async with _client() as client:
        response = await client.get(_CHECK_PATH)

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "ibkr_no_bars"


async def test_release_after_a_passing_check_removes_the_hold_and_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _held_lane(tmp_path)
    _gateway(monkeypatch, _bars())

    async with _client() as client:
        assert (await client.get(_CHECK_PATH)).status_code == 200
        response = await client.post(_RELEASE_PATH, json=_BODY)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["was_held"] is True
    assert body["marker"]["volume"] == "learn-ai-alpaca-clerk-data"
    assert body["bar_check"]["bar_count"] == 1
    assert read_go_live_hold(_volume(tmp_path)).held is False
    assert len(list((_volume(tmp_path) / GO_LIVE_RECEIPTS_DIRECTORY).glob("*.json"))) == 1


async def test_the_bar_check_alone_does_not_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _held_lane(tmp_path)
    _gateway(monkeypatch, _bars())

    async with _client() as client:
        assert (await client.get(_CHECK_PATH)).status_code == 200
        missing = await client.post(
            _RELEASE_PATH, json={"operator": "inkant", "change_ref": "go-live"}
        )
        wrong = await client.post(
            _RELEASE_PATH, json={**_BODY, "old_machine_off_confirmation": "yes"}
        )

    assert missing.status_code == 422
    assert wrong.status_code == 422
    assert read_go_live_hold(_volume(tmp_path)).held is True


async def test_the_confirmation_alone_does_not_release(tmp_path: Path) -> None:
    _held_lane(tmp_path)

    async with _client() as client:
        response = await client.post(_RELEASE_PATH, json=_BODY)

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "lane_go_live_bar_check_required"
    assert read_go_live_hold(_volume(tmp_path)).held is True


async def test_release_without_a_bot_runner_refuses() -> None:
    async with _client() as client:
        response = await client.post(_RELEASE_PATH, json=_BODY)

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "lane_bot_runner_not_installed"


async def test_both_routes_are_guarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "test-control-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    _held_lane(tmp_path)
    _gateway(monkeypatch, _bars())

    async with _client() as client:
        check = await client.get(_CHECK_PATH)
        release = await client.post(_RELEASE_PATH, json=_BODY)

    assert check.status_code == 403
    assert release.status_code == 403
    assert read_go_live_hold(_volume(tmp_path)).held is True


async def test_a_marker_that_cannot_be_removed_is_a_named_503_not_a_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _held_lane(tmp_path)
    _gateway(monkeypatch, _bars())
    real_unlink = Path.unlink

    def refusing_unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == GO_LIVE_HOLD_MARKER:
            raise PermissionError(13, "Permission denied")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refusing_unlink)

    async with _client() as client:
        assert (await client.get(_CHECK_PATH)).status_code == 200
        response = await client.post(_RELEASE_PATH, json=_BODY)

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "lane_go_live_release_failed"
    assert read_go_live_hold(_volume(tmp_path)).held is True


async def test_a_lane_that_cannot_name_its_clerk_volume_does_not_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _held_lane(tmp_path)
    _gateway(monkeypatch, _bars())
    monkeypatch.setenv(CLERK_DIR_ENV_VAR, "relative/clerk")

    async with _client() as client:
        assert (await client.get(_CHECK_PATH)).status_code == 200
        response = await client.post(_RELEASE_PATH, json=_BODY)

    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "lane_go_live_hold_unreadable"
    assert read_go_live_hold(_volume(tmp_path)).held is True
