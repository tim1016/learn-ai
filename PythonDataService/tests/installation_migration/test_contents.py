"""The bundle's contents pin the committed Compose topology (#2268).

Owner rule: every podman volume and every gitignored folder a container
mounts goes in the bundle. The list is declared once, in
``app.installation_migration.contents``; these tests fail the moment the
committed topology gains a mounted volume or gitignored bind that the list
neither carries nor excludes with a stated reason — so a new mount cannot be
silently left behind on the old machine.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from app.installation_migration.contents import (
    BUNDLED_FOLDERS,
    BUNDLED_VOLUMES,
    EXCLUDED_BIND_MOUNTS,
    LAKE_FOLDER_KEY,
    is_secret_shaped,
    resolve_folder_path,
)
from app.installation_migration.tree import require_bundleable
from app.lean_sidecar.launcher_auth import LAUNCHER_TOKEN_FILENAME

_REPO = Path(__file__).resolve().parents[3]
_PROJECT = "learn-ai"
_COMPOSE_FILES = ("compose.yaml", "compose.fleet.dev.yaml")
_DEFAULTED = re.compile(r"^\$\{[A-Z0-9_]+:-(?P<default>[^}]+)\}$")


class _TagTolerantLoader(yaml.SafeLoader):
    """Parse Compose's ``!override``/``!reset`` merge tags as plain values."""


def _plain(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node) -> object:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_TagTolerantLoader.add_multi_constructor("!", _plain)


def _compose(name: str) -> dict:
    return yaml.load((_REPO / name).read_text(encoding="utf-8"), Loader=_TagTolerantLoader)


def _declared_volume_names() -> set[str]:
    names: set[str] = set()
    for compose_file in _COMPOSE_FILES:
        for key, spec in (_compose(compose_file).get("volumes") or {}).items():
            explicit = (spec or {}).get("name")
            names.add(explicit or f"{_PROJECT}_{key}")
    return names


def _bind_sources() -> set[str]:
    """Every repo-relative bind source any service in the dev topology mounts."""
    sources: set[str] = set()
    for compose_file in _COMPOSE_FILES:
        for service in (_compose(compose_file).get("services") or {}).values():
            for mount in service.get("volumes") or []:
                text = str(mount)
                source = (
                    text[: text.index("}") + 1]
                    if text.startswith("${")
                    else text.split(":", 1)[0]
                )
                match = _DEFAULTED.match(source)
                if match:
                    source = match.group("default")
                if not source.startswith((".", "/")):
                    continue  # a named volume
                sources.add(source.removeprefix("./").rstrip("/"))
    return sources


def _gitignored(path: str) -> bool:
    if path.startswith(".."):
        return False
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", f"{path}/probe"],
        cwd=_REPO,
        check=False,
        timeout=30,
    )
    return result.returncode == 0


def test_bundled_volumes_are_exactly_the_topology_named_volumes() -> None:
    import json

    snapshot = json.loads(
        (_REPO / "deploy" / "fleet" / "topology.snapshot.json").read_text(encoding="utf-8")
    )
    rendered = {spec["name"] for spec in snapshot["volumes"].values()}

    assert {volume.name for volume in BUNDLED_VOLUMES} == _declared_volume_names(), (
        "bundled volumes are exactly the topology-named volumes"
    )
    assert rendered <= {volume.name for volume in BUNDLED_VOLUMES}, (
        "bundled volume names cover the rendered snapshot"
    )


def test_every_gitignored_bind_is_bundled_or_excluded_with_a_reason() -> None:
    folders = {folder.key for folder in BUNDLED_FOLDERS}
    sources = _bind_sources()
    ignored = {source for source in sources if _gitignored(source)}

    unaccounted = {
        source
        for source in ignored - set(EXCLUDED_BIND_MOUNTS)
        # A bind nested inside a bundled folder travels with that folder.
        if not any(source == key or source.startswith(f"{key}/") for key in folders)
    }

    assert unaccounted == set(), "every gitignored bind is bundled or excluded"
    assert folders <= ignored, "a bundled folder must be a gitignored mounted folder"
    for source, reason in EXCLUDED_BIND_MOUNTS.items():
        assert source in sources, f"the exclusion {source} is a real mount"
        assert reason.strip(), f"the exclusion {source} states a reason"


def test_the_owner_listed_contents_are_all_present_as_distinct_bundle_members() -> None:
    members = [volume.member for volume in BUNDLED_VOLUMES] + [
        folder.member for folder in BUNDLED_FOLDERS
    ]

    assert {volume.name for volume in BUNDLED_VOLUMES} == {
        "learn-ai_pgdata",
        "learn-ai_alpaca-fleet-control",
        "learn-ai-alpaca-clerk-data",
        "learn-ai-alpaca-paper-clerk-data",
    }, "the owner-listed volumes are all present"
    # Owner rule: every gitignored folder a container mounts goes in — which
    # carries the derived analytics cache too.
    assert {folder.key for folder in BUNDLED_FOLDERS} == {
        "data-lake-volume",
        "PythonDataService/artifacts",
        "PythonDataService/cache",
        "PythonDataService/lean-cache",
    }, "the owner-listed folders are all present"
    assert set(EXCLUDED_BIND_MOUNTS) == {"../Lean/Data"}, "the owner-listed exclusions are all present"
    assert len(set(members)) == len(members), "members are distinct paths inside the bundle"
    assert all(member.startswith(("volumes/", "folders/")) for member in members), (
        "members sit under volumes/ or folders/"
    )


def test_the_lake_path_comes_from_the_repo_root_dotenv_as_compose_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lake = next(folder for folder in BUNDLED_FOLDERS if folder.key == LAKE_FOLDER_KEY)
    monkeypatch.delenv("LEAN_DATA_VOLUME_HOST_PATH", raising=False)
    (tmp_path / ".env").write_text(
        f"export LEAN_DATA_VOLUME_HOST_PATH={tmp_path / 'from-dotenv'}  # the big disk\n",
        encoding="utf-8",
    )

    assert resolve_folder_path(tmp_path, lake, lake_dir=None) == tmp_path / "from-dotenv"

    # The process environment wins over .env, exactly as in Compose ...
    monkeypatch.setenv("LEAN_DATA_VOLUME_HOST_PATH", str(tmp_path / "from-env"))
    assert resolve_folder_path(tmp_path, lake, lake_dir=None) == tmp_path / "from-env"
    # ... even when it is set but empty, which ``${VAR:-default}`` then defaults.
    monkeypatch.setenv("LEAN_DATA_VOLUME_HOST_PATH", "")
    assert resolve_folder_path(tmp_path, lake, lake_dir=None) == tmp_path / "data-lake-volume"


def test_the_lake_folder_follows_its_host_path_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lake = next(folder for folder in BUNDLED_FOLDERS if folder.key == LAKE_FOLDER_KEY)
    monkeypatch.delenv("LEAN_DATA_VOLUME_HOST_PATH", raising=False)
    assert resolve_folder_path(tmp_path, lake, lake_dir=None) == tmp_path / "data-lake-volume"

    monkeypatch.setenv("LEAN_DATA_VOLUME_HOST_PATH", str(tmp_path / "elsewhere"))
    assert resolve_folder_path(tmp_path, lake, lake_dir=None) == tmp_path / "elsewhere"
    assert resolve_folder_path(tmp_path, lake, lake_dir=tmp_path / "flag") == tmp_path / "flag"


@pytest.mark.parametrize(
    ("name", "secret_shaped"),
    [
        pytest.param(".env", True, id="secret-env"),
        pytest.param("live.env", True, id="secret-live_env"),
        pytest.param("compose.override.yaml", True, id="secret-compose_override_yaml"),
        pytest.param("compose.override.yml", True, id="secret-compose_override_yml"),
        pytest.param(LAUNCHER_TOKEN_FILENAME, True, id="secret-launcher_token"),
        pytest.param(".host-daemon-token", True, id="secret-host_daemon_token"),
        pytest.param("coordinator-token", True, id="secret-coordinator_token"),
        pytest.param("service_token", True, id="secret-service_token"),
        pytest.param(".clerk-host-binding-capability", True, id="secret-clerk_host_binding_capability"),
        pytest.param("server.pem", True, id="secret-pem"),
        pytest.param("signing.key", True, id="secret-key"),
        pytest.param("id_rsa", True, id="secret-id_rsa"),
        pytest.param("id_ed25519.pub", True, id="secret-id_ed25519_pub"),
        pytest.param("client.p12", True, id="secret-p12"),
        pytest.param("broker.pfx", True, id="secret-pfx"),
        pytest.param("truststore.jks", True, id="secret-jks"),
        pytest.param("alpaca_secret.json", True, id="secret-alpaca_secret_json"),
        pytest.param("DB_PASSWORD.txt", True, id="secret-db_password_txt"),
        pytest.param("gcp-credentials.json", True, id="secret-gcp_credentials_json"),
        pytest.param("live.env.example", False, id="ordinary-live_env_example"),
        pytest.param("environment.txt", False, id="ordinary-environment_txt"),
        pytest.param("tokenizer.json", False, id="ordinary-tokenizer_json"),
        pytest.param("run.json", False, id="ordinary-run_json"),
        pytest.param("keys.parquet", False, id="ordinary-keys_parquet"),
        pytest.param("identity.json", False, id="ordinary-identity_json"),
        pytest.param("SPY_minute.zip", False, id="ordinary-spy_minute_zip"),
    ],
)
def test_secret_shaped_names(name: str, secret_shaped: bool) -> None:
    assert is_secret_shaped(name) == secret_shaped


def test_the_auth_tokens_under_artifacts_are_secret_shaped_and_skipped(tmp_path: Path) -> None:
    """All three exist on the owner's host today (#2269): skipped and named."""
    artifacts = tmp_path / "PythonDataService" / "artifacts"
    (artifacts / "lean-sidecar").mkdir(parents=True)
    (artifacts / "lean-sidecar" / LAUNCHER_TOKEN_FILENAME).write_text("t", encoding="utf-8")
    (artifacts / ".host-daemon-token").write_text("t", encoding="utf-8")
    (artifacts / ".clerk-host-binding-capability").write_text("t", encoding="utf-8")
    (artifacts / "run.json").write_text("{}", encoding="utf-8")

    skipped = require_bundleable({"PythonDataService/artifacts": artifacts})

    assert skipped == [
        "PythonDataService/artifacts/.clerk-host-binding-capability",
        "PythonDataService/artifacts/.host-daemon-token",
        "PythonDataService/artifacts/lean-sidecar/.launcher-token",
    ]
