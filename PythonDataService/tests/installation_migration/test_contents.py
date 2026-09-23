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
from app.installation_migration.errors import MigrationRefused
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
    assert {volume.name for volume in BUNDLED_VOLUMES} == _declared_volume_names()


def test_bundled_volume_names_match_the_rendered_snapshot() -> None:
    import json

    snapshot = json.loads(
        (_REPO / "deploy" / "fleet" / "topology.snapshot.json").read_text(encoding="utf-8")
    )
    rendered = {spec["name"] for spec in snapshot["volumes"].values()}

    assert rendered <= {volume.name for volume in BUNDLED_VOLUMES}


def test_every_gitignored_bind_is_bundled_or_excluded_with_a_reason() -> None:
    folders = {folder.key for folder in BUNDLED_FOLDERS}
    ignored = {source for source in _bind_sources() if _gitignored(source)}

    unaccounted = {
        source
        for source in ignored - set(EXCLUDED_BIND_MOUNTS)
        # A bind nested inside a bundled folder travels with that folder.
        if not any(source == key or source.startswith(f"{key}/") for key in folders)
    }

    assert unaccounted == set()
    assert folders <= ignored, "a bundled folder must be a gitignored mounted folder"


def test_every_exclusion_is_a_real_mount_with_a_reason() -> None:
    sources = _bind_sources()
    for source, reason in EXCLUDED_BIND_MOUNTS.items():
        assert source in sources
        assert reason.strip()


def test_the_owner_listed_contents_are_all_present() -> None:
    assert {volume.name for volume in BUNDLED_VOLUMES} == {
        "learn-ai_pgdata",
        "learn-ai_alpaca-fleet-control",
        "learn-ai-alpaca-clerk-data",
        "learn-ai-alpaca-paper-clerk-data",
        "learn-ai-alpaca-clerk-qualification-data",
    }
    assert {folder.key for folder in BUNDLED_FOLDERS} == {
        "data-lake-volume",
        "PythonDataService/artifacts",
        "PythonDataService/lean-cache",
    }


def test_members_are_distinct_paths_inside_the_bundle() -> None:
    members = [volume.member for volume in BUNDLED_VOLUMES] + [
        folder.member for folder in BUNDLED_FOLDERS
    ]

    assert len(set(members)) == len(members)
    assert all(member.startswith(("volumes/", "folders/")) for member in members)


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
    "name",
    [
        ".env",
        "live.env",
        "compose.override.yaml",
        "compose.override.yml",
        LAUNCHER_TOKEN_FILENAME,
        ".host-daemon-token",
        "coordinator-token",
        "service_token",
        ".clerk-host-binding-capability",
        "server.pem",
        "signing.key",
    ],
)
def test_secret_shaped_names(name: str) -> None:
    assert is_secret_shaped(name)


@pytest.mark.parametrize(
    "name",
    ["live.env.example", "environment.txt", "tokenizer.json", "run.json", "keys.parquet"],
)
def test_ordinary_names_are_not_secret_shaped(name: str) -> None:
    assert not is_secret_shaped(name)


def test_the_live_auth_tokens_under_artifacts_are_secret_shaped(tmp_path: Path) -> None:
    """Both exist on the owner's host today and are live authentication tokens."""
    artifacts = tmp_path / "PythonDataService" / "artifacts"
    (artifacts / "lean-sidecar").mkdir(parents=True)
    (artifacts / "lean-sidecar" / LAUNCHER_TOKEN_FILENAME).write_text("t", encoding="utf-8")
    (artifacts / ".host-daemon-token").write_text("t", encoding="utf-8")
    (artifacts / "run.json").write_text("{}", encoding="utf-8")

    with pytest.raises(MigrationRefused) as refused:
        require_bundleable([artifacts])

    assert refused.value.reason == "secret_in_bundle_source"
    assert sorted(Path(path).name for path in refused.value.details["paths"]) == [
        ".host-daemon-token",
        ".launcher-token",
    ]
