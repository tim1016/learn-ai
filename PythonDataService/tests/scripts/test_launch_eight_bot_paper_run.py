"""Regression coverage for the eight-bot paper-run launcher."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts import launch_eight_bot_paper_run as launcher


class _RecordingClient:
    """Minimal synchronous client double that records launcher configuration."""

    init_kwargs: dict[str, Any]
    posted_payloads: list[dict[str, Any]]

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.posted_payloads = []

    def __enter__(self) -> _RecordingClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def post(self, _: str, *, json: dict[str, Any]) -> SimpleNamespace:
        self.posted_payloads.append(json)
        return SimpleNamespace(
            status_code=201,
            text="",
            json=lambda: {
                "run_id": "run-id-for-launcher-test",
                "created": True,
                "start": {"accepted": True},
            },
        )


def test_main_loads_normalized_secret_from_env_file_and_configures_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DATA_PLANE_CONTROL_SECRET=  launcher-secret  \n", encoding="utf-8")
    monkeypatch.delenv("DATA_PLANE_CONTROL_SECRET", raising=False)
    clients: list[_RecordingClient] = []

    def create_client(**kwargs: Any) -> _RecordingClient:
        client = _RecordingClient(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(launcher.httpx, "Client", create_client)
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)

    exit_code = launcher.main(["--date", "20260730", "--stagger", "0", "--env-file", str(env_file)])

    assert exit_code == 0
    assert len(clients) == 1
    client = clients[0]
    assert client.init_kwargs == {
        "base_url": "http://127.0.0.1:8000",
        "timeout": 120.0,
        "headers": {"X-Data-Plane-Control-Secret": "launcher-secret"},
    }
    assert len(client.posted_payloads) == 8


def test_main_refuses_live_deploy_when_control_secret_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("POLYGON_API_KEY=test-key\n", encoding="utf-8")
    monkeypatch.delenv("DATA_PLANE_CONTROL_SECRET", raising=False)
    client_created = False

    def fail_if_client_is_created(**_: Any) -> _RecordingClient:
        nonlocal client_created
        client_created = True
        raise AssertionError("live deployment must fail before constructing an HTTP client")

    monkeypatch.setattr(launcher.httpx, "Client", fail_if_client_is_created)

    exit_code = launcher.main(["--env-file", str(env_file)])

    assert exit_code == 2
    assert client_created is False
