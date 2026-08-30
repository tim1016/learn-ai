"""Regression test: claim_minute_bar raises CatalogSchemaNotReadyError, not a
raw asyncpg exception, when the DB schema is one migration behind the code.

Reproduces the deploy-ordering race CatalogSchemaNotReadyError exists to
make legible (app/data_lake/catalog_client.py): compose.yaml health-gates
Backend on python-service, not the reverse, and Backend applies its EF Core
migrations during its own startup. So on any deploy that ships
20260830120000_ActivateDataRootScopedCatalogIdentity (issue #1878, PR B of
#1861 -- Codex P2 finding on PR #1883), there is a real window where
python-service is already serving /ensure-data traffic while Postgres is
still sitting on the prior migration, whose partial-unique indexes don't
lead with DataRootId. claim_minute_bar's ON CONFLICT target does lead with
it, so Postgres raises SQLSTATE 42P10 (no unique/exclusion constraint
matches the conflict target) until the migration finishes.

This deliberately does NOT reuse the shared POSTGRES_URL instance the rest
of the suite runs against -- CI's "Python Tests" job migrates that instance
to head (`dotnet ef database update` with no target) before pytest even
starts, which is exactly the state this test must NOT be in. Instead it
provisions its own disposable Postgres, pinned one migration behind, and
tears it down when done. Never points at my-postgres, the shared dev
container.

Skips cleanly (same convention as test_schema_drift.py /
test_catalog_write_ops.py) when POSTGRES_URL is unset, or when podman/dotnet
aren't on PATH -- both true on a plain local checkout. CI's "Python Tests"
job sets POSTGRES_URL and installs the .NET SDK + dotnet-ef before running
pytest (see .github/workflows/ci.yml), and GitHub-hosted ubuntu-latest
runners ship podman preinstalled, so this test actually executes in CI
rather than skipping silently.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import date
from pathlib import Path

import asyncpg
import pytest

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.catalog_client import CatalogSchemaNotReadyError
from app.data_lake.types import ArtifactIdentity

pytestmark = pytest.mark.asyncio

# tests/integration/data_lake/<this file> -> PythonDataService -> repo root.
REPO_ROOT = Path(__file__).resolve().parents[4]
BACKEND_PROJECT = REPO_ROOT / "Backend" / "Backend.csproj"
MIGRATIONS_DIR = REPO_ROOT / "Backend" / "Migrations"
TARGET_MIGRATION_CLASS = "ActivateDataRootScopedCatalogIdentity"

_MIGRATION_FILENAME_RE = re.compile(r"^(\d{14})_([A-Za-z0-9]+)\.cs$")


def _prior_migration_name() -> str:
    """The EF migration class immediately before TARGET_MIGRATION_CLASS.

    Discovered by listing Backend/Migrations/*.cs (excluding .Designer.cs
    and the model snapshot) and sorting on the timestamp prefix, rather than
    hardcoded, so this test does not silently start targeting the wrong
    migration if one is renamed or inserted.
    """
    entries: list[tuple[str, str]] = []
    for path in MIGRATIONS_DIR.glob("*.cs"):
        if path.name.endswith(".Designer.cs") or path.name == "AppDbContextModelSnapshot.cs":
            continue
        match = _MIGRATION_FILENAME_RE.match(path.name)
        if match is not None:
            entries.append((match.group(1), match.group(2)))
    entries.sort()
    class_names = [name for _, name in entries]
    idx = class_names.index(TARGET_MIGRATION_CLASS)
    assert idx > 0, f"{TARGET_MIGRATION_CLASS} has no prior migration under {MIGRATIONS_DIR}"
    return class_names[idx - 1]


def _dotnet_subprocess_env() -> dict[str, str]:
    """subprocess env with ~/.dotnet/tools on PATH.

    CI's "Migrate the data-lake schema" step installs the dotnet-ef global
    tool and exports PATH inline within its own `run:` step -- that export
    does not persist to the later Python Tests step this pytest process runs
    in, but the tool binary it installed is still on disk. Prepending its
    known install location here makes `dotnet ef` resolve regardless of
    which step (or which local shell) is invoking this test.
    """
    env = os.environ.copy()
    tools_dir = str(Path.home() / ".dotnet" / "tools")
    env["PATH"] = tools_dir + os.pathsep + env.get("PATH", "")
    return env


async def _wait_for_postgres_ready(dsn: str, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_exc: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            conn = await asyncpg.connect(dsn, timeout=2)
        except (OSError, asyncpg.PostgresError) as exc:
            last_exc = exc
            await asyncio.sleep(0.5)
            continue
        await conn.close()
        return
    pytest.fail(f"disposable postgres container never became ready: {last_exc}")


@pytest.fixture
async def pre_migration_dsn():
    """Yield an asyncpg DSN for a disposable Postgres migrated only through
    the migration immediately before ActivateDataRootScopedCatalogIdentity.

    Skips (does not fail) when the environment lacks a capability this test
    needs at all: POSTGRES_URL unset (Postgres-gated tests disabled, same
    convention as the rest of this directory), or podman/dotnet missing from
    PATH. Once those are confirmed present, a failure provisioning the
    container or applying the migration is a real test failure, not a skip
    -- otherwise a genuine bug here would silently stop covering anything.
    """
    if not (settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")):
        pytest.skip("POSTGRES_URL not configured; Postgres-gated tests are disabled in this environment")
    if shutil.which("podman") is None:
        pytest.skip("podman not on PATH; cannot provision a disposable pre-migration Postgres for this test")
    if shutil.which("dotnet") is None:
        pytest.skip("dotnet not on PATH; cannot apply EF migrations for this test")

    env = _dotnet_subprocess_env()
    ef_check = subprocess.run(
        ["dotnet", "ef", "--version"], capture_output=True, text=True, timeout=60, env=env
    )
    if ef_check.returncode != 0:
        pytest.skip(
            "dotnet-ef global tool not installed (`dotnet tool install --global dotnet-ef`); "
            "cannot apply EF migrations for this test"
        )

    prior_migration = _prior_migration_name()
    container_name = f"catalog-schema-not-ready-{uuid.uuid4().hex[:10]}"

    started = subprocess.run(
        [
            "podman",
            "run",
            "-d",
            "--rm",
            "--name",
            container_name,
            "-e",
            "POSTGRES_PASSWORD=postgres",
            "-p",
            "127.0.0.1::5432",
            "postgres:16",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if started.returncode != 0:
        pytest.skip(f"could not start a disposable Postgres via podman: {started.stderr.strip()}")

    try:
        port_result = subprocess.run(
            ["podman", "port", container_name, "5432"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert port_result.returncode == 0, f"podman port failed: {port_result.stderr}"
        port = int(port_result.stdout.strip().rsplit(":", 1)[-1])
        dsn = f"postgres://postgres:postgres@127.0.0.1:{port}/postgres"
        connection_string = f"Host=127.0.0.1;Port={port};Database=postgres;Username=postgres;Password=postgres"

        await _wait_for_postgres_ready(dsn)

        build = subprocess.run(
            ["dotnet", "build", str(BACKEND_PROJECT)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        assert build.returncode == 0, f"dotnet build Backend/Backend.csproj failed:\n{build.stdout}\n{build.stderr}"

        migrate = subprocess.run(
            [
                "dotnet",
                "ef",
                "database",
                "update",
                prior_migration,
                "--project",
                str(BACKEND_PROJECT),
                "--connection",
                connection_string,
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        assert migrate.returncode == 0, (
            f"dotnet ef database update {prior_migration} failed:\n{migrate.stdout}\n{migrate.stderr}"
        )

        yield dsn
    finally:
        subprocess.run(
            ["podman", "stop", "-t", "5", container_name],
            capture_output=True,
            text=True,
            timeout=30,
        )


def _minute_identity() -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_kind="time_series_bars",
        market="usa",
        symbol="SPY",
        trading_date=date(2024, 5, 20),
        resolution="minute",
        data_type="trade",
        provider="polygon",
        price_adjustment_mode="raw",
    )


async def test_claim_minute_bar_raises_catalog_schema_not_ready_pre_migration(
    pre_migration_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "POSTGRES_URL", pre_migration_dsn)
    await catalog_client.close_pool()
    await catalog_client.init_pool()
    try:
        with pytest.raises(CatalogSchemaNotReadyError) as exc_info:
            await catalog_client.claim_minute_bar(
                identity=_minute_identity(),
                worker_id="w-1",
                lease_ttl_ms=300_000,
                data_contract_hash="a" * 64,
                file_path="equity/usa/minute/spy/20240520_trade.zip",
            )

        # Translated, not the raw driver error -- and the chained cause is
        # exactly the SQLSTATE this whole test exists to pin.
        assert not isinstance(exc_info.value, asyncpg.PostgresError)
        assert "42P10" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, asyncpg.PostgresError)
        assert exc_info.value.__cause__.sqlstate == "42P10"
    finally:
        await catalog_client.close_pool()
