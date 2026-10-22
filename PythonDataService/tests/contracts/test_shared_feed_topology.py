"""Fleet cutover preserves provider, credentials, custody and Gateway isolation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest
from dotenv import dotenv_values

from app.broker.ibkr.config import IbkrSettings
from tests.contracts.compose_files import ROOT, render_module


@pytest.mark.parametrize("name", ["compose.fleet.dev.yaml", "compose.fleet.yaml"])
def test_each_fleet_uses_one_private_readonly_owner_without_execution_credentials(name: str) -> None:
    document = render_module().load_compose_document(ROOT / name)
    source = document["services"]["ibkr-market-data"]
    environment = source["environment"]
    assert environment["IBKR_BROKER_ENABLED"] == environment["IBKR_READONLY"] == "true"
    assert "app.marketdata.shared.server:app" in source["command"]
    assert source["read_only"] and source["pids_limit"] > 0
    assert not source.get("ports") and not source.get("volumes")
    assert not any(key.startswith("ALPACA_") for key in environment)
    assert "IBKR_SHARED_FEED_TOKEN" not in environment
    assert set(dotenv_values(ROOT / "deploy/fleet/env/market-data.env.example")) == {"IBKR_SHARED_FEED_TOKEN"}
    client_ids = {environment["IBKR_CLIENT_ID"].split(":-")[-1].rstrip("}")}
    for lane in ["alpaca-live-clerk", "alpaca-paper-clerk"]:
        spec = document["services"][lane]
        env = spec["environment"]
        assert env["IBKR_BROKER_ENABLED"] == env["IBKR_READONLY"] == "true"
        assert env["IBKR_SHARED_FEED_URL"] == "http://ibkr-market-data:8000"
        assert "IBKR_SHARED_FEED_TOKEN" not in env
        example = dotenv_values(
            ROOT / "deploy/fleet/env" / lane.replace("alpaca-", "").replace("-clerk", ".env.example")
        )
        assert "IBKR_SHARED_FEED_TOKEN" in example
        assert spec["depends_on"]["ibkr-market-data"]["condition"] == "service_healthy"
        client_ids.add(env.get("IBKR_CLIENT_ID", example["IBKR_CLIENT_ID"]).split(":-")[-1].rstrip("}"))
    assert len(client_ids) == 3


def test_qualification_render_and_worker_settings_cannot_reach_the_real_source(tmp_path: object) -> None:


    engine = shutil.which("docker") or shutil.which("podman")
    if engine is None:
        pytest.skip("Compose renderer is unavailable")
    temporary = tmp_path
    environment = {**os.environ}
    for role in ["PAPER", "LIVE", "COORDINATOR"]:
        path = temporary / f"{role}.env"
        path.write_text(
            "IBKR_CLIENT_ID=1201\n" if role == "PAPER" else "IBKR_CLIENT_ID=1202\n" if role == "LIVE" else ""
        )
        environment[f"FLEET_{role}_ENV_FILE"] = str(path)
    environment["FLEET_MARKET_DATA_ENV_FILE"] = str(temporary / "absent-production-source.env")
    result = subprocess.run(
        [
            engine,
            "compose",
            "--env-file",
            str(ROOT / "deploy/fleet/ci-render.placeholders"),
            "-f",
            str(ROOT / "compose.fleet.yaml"),
            "-f",
            str(ROOT / "compose.fleet.qualification.yaml"),
            "--profile",
            "fleet-qualification",
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    services = json.loads(result.stdout)["services"]
    assert "ibkr-market-data" not in services
    for lane, client_id in [("paper", 1201), ("live", 1202)]:
        service = services[f"alpaca-{lane}-clerk"]
        assert "ibkr-market-data" not in service["depends_on"]
        env = service["environment"]
        assert not env.get("IBKR_SHARED_FEED_URL") and not env.get("IBKR_SHARED_FEED_TOKEN")
        settings = IbkrSettings(
            _env_file=None,
            broker_enabled=env["IBKR_BROKER_ENABLED"],
            client_id=env["IBKR_CLIENT_ID"],
            shared_feed_url=env.get("IBKR_SHARED_FEED_URL"),
            shared_feed_token=None,
        )
        assert not settings.broker_enabled and settings.shared_feed_url is None and settings.client_id == client_id
