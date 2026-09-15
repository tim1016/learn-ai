"""Render the committed fleet topology to a secret-free, committable snapshot.

`compose config` inlines every env_file value into its output, so the rendered
document itself is never safe to commit. This projects it down to what a
topology review actually needs — services, images, env KEY NAMES, mount
sources and targets, networks, ports and containment settings — and drops
every value on the way through.

CI renders with `docker compose` (what ubuntu-latest ships); the dev host
renders with `podman compose`. The two engines can disagree on `!override`
merge order, `deploy.resources` vs top-level `cpus`/`mem_limit`, and `:z`
relabel suffixes, so `--engine` is a required choice, not a convenience — the
host migration runbook uses `--engine "podman compose" --check` for exactly
this reason (see docs/runbooks/fleet-dev-two-lane-posture.md).
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = ("compose.yaml", "compose.fleet.dev.yaml")
SNAPSHOT_PATH = REPOSITORY_ROOT / "deploy" / "fleet" / "topology.snapshot.json"
PLACEHOLDERS = REPOSITORY_ROOT / "deploy" / "fleet" / "ci-render.placeholders"
CONTAINMENT_FIELDS = ("read_only", "pids_limit", "tmpfs", "privileged", "cap_add")

# cpus/memory limits arrive in one of two shapes depending on which form the
# compose file uses and how the engine renders it: the short top-level
# `cpus:`/`mem_limit:` syntax, or `deploy.resources.limits.{cpus,memory}` --
# the form the clerk overlay actually uses. Reading only the top-level fields
# (as CONTAINMENT_FIELDS did before) records `{}` for every service that uses
# `deploy:`, which both fleet clerks do -- deleting `cpus: 1.0` or
# `memory: 768m` then produces no diff despite the renderer claiming to pin
# containment. (top-level rendered field, deploy.resources.limits field)
_RESOURCE_LIMIT_SOURCES = (("cpus", "cpus"), ("mem_limit", "memory"))

# compose.fleet.dev.yaml's live/paper env_file entries are `required: true`,
# so `compose config` fails outright if the real, gitignored
# deploy/fleet/env/{live,paper}.env do not exist — true on a fresh clone and
# in CI. The tracked `.example` files declare the identical key set with
# blank values, so pointing the render at them (only if the operator hasn't
# already pointed at something else) makes the render deterministic,
# secret-free, and reproducible anywhere without any host-side ceremony.
_ENV_FILE_DEFAULTS = {
    "FLEET_LIVE_ENV_FILE": "./deploy/fleet/env/live.env.example",
    "FLEET_PAPER_ENV_FILE": "./deploy/fleet/env/paper.env.example",
    "FLEET_COORDINATOR_ENV_FILE": "./deploy/fleet/env/coordinator.env.example",
}

# Non-secret fence facts worth checking without ever persisting a raw
# environment value. ALPACA_CLERK_DIR="" on the coordinator neutralizes the
# base clerk path so no clerk surface can reappear there; this turns that
# invariant from a comment into a checked, still secret-free fact.
_FENCE_KEYS = ("ALPACA_CLERK_DIR",)


class ComposeTagTolerantLoader(yaml.SafeLoader):
    """Parses Compose's `!override`/`!reset` merge tags as plain values.

    Those tags only affect how Compose merges *multiple* files together; the
    static (unmerged, unexpanded) read of a single file below only needs the
    tagged node to parse, not the merge semantics.
    """


def _construct_tagged_node(loader: yaml.SafeLoader, node: yaml.Node) -> object:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


ComposeTagTolerantLoader.add_constructor("!override", _construct_tagged_node)
ComposeTagTolerantLoader.add_constructor("!reset", _construct_tagged_node)


def load_compose_document(path: Path) -> dict[str, object]:
    """Parse one compose file on its own, tolerating Compose's merge tags."""
    return yaml.load(path.read_text(encoding="utf-8"), Loader=ComposeTagTolerantLoader)


def _raw_env_file_paths() -> dict[str, list[str]]:
    """The `env_file:` `path:` entries as *written* (unexpanded, no default
    resolved) — `compose config`'s JSON output drops `env_file` entirely once
    it folds the values into `environment`, so this reads the committed
    source directly. The raw `${VAR:-default}` text is not a secret and,
    unlike the resolved path, does not depend on which env-file override this
    render happened to use.
    """
    paths: dict[str, list[str]] = {}
    for name in COMPOSE_FILES:
        document = load_compose_document(REPOSITORY_ROOT / name)
        for service, spec in (document.get("services") or {}).items():
            for entry in spec.get("env_file") or []:
                path = entry.get("path") if isinstance(entry, dict) else entry
                paths.setdefault(service, []).append(path)
    return {service: sorted(entries) for service, entries in paths.items()}


def _resource_limits(spec: dict[str, object]) -> dict[str, str]:
    """Normalise cpus/memory limits across both rendered shapes into one
    stable, stringified form -- so a limit expressed either way is pinned,
    and changing or deleting it (whichever form it's in) shows up as a diff.
    """
    deploy_limits = ((spec.get("deploy") or {}).get("resources") or {}).get("limits") or {}
    limits: dict[str, str] = {}
    for top_level_field, deploy_field in _RESOURCE_LIMIT_SOURCES:
        value = spec.get(top_level_field, deploy_limits.get(deploy_field))
        if value is not None:
            limits[top_level_field] = str(value)
    return limits


def _relative_mount_source(mount: dict[str, object]) -> str:
    source = mount.get("source")
    if mount.get("type") != "bind" or not isinstance(source, str) or not source.startswith("/"):
        return str(source)
    # Bind sources render as absolute host paths rooted at whatever directory
    # the render happened to run from. Expressed relative to the repository
    # root instead, the snapshot is identical from a fresh clone, a worktree,
    # or CI's runner — none of which share the same absolute prefix.
    return os.path.relpath(source, REPOSITORY_ROOT)


def render(engine: list[str]) -> dict[str, object]:
    command = [*engine, "--project-name", "learn-ai",
               "--project-directory", str(REPOSITORY_ROOT),
               "--env-file", str(PLACEHOLDERS)]
    for name in COMPOSE_FILES:
        command += ["--file", str(REPOSITORY_ROOT / name)]
    command += ["config", "--format", "json"]
    render_environment = {**os.environ}
    for key, default in _ENV_FILE_DEFAULTS.items():
        render_environment.setdefault(key, default)
    completed = subprocess.run(
        command, check=True, capture_output=True, text=True, timeout=180,
        cwd=REPOSITORY_ROOT, env=render_environment,
    )
    document = json.loads(completed.stdout)
    env_file_paths = _raw_env_file_paths()
    detail: dict[str, object] = {}
    fence: dict[str, dict[str, bool]] = {}
    for name, spec in sorted((document.get("services") or {}).items()):
        environment = spec.get("environment") or {}
        detail[name] = {
            "image": spec.get("image"),
            "container_name": spec.get("container_name"),
            "environment_keys": sorted(environment),
            "env_file": env_file_paths.get(name, []),
            "volumes": sorted(
                f"{_relative_mount_source(mount)}->{mount.get('target')}:{mount.get('type')}"
                for mount in (spec.get("volumes") or [])
            ),
            "networks": sorted(spec.get("networks") or {}),
            "ports": sorted(
                f"{port.get('host_ip')}:{port.get('published')}->{port.get('target')}"
                for port in (spec.get("ports") or [])
            ),
            "containment": {
                **{field: spec[field] for field in CONTAINMENT_FIELDS if field in spec},
                **_resource_limits(spec),
            },
        }
        # Derived boolean only — never the value itself, secret or not.
        fence[name] = {key: environment.get(key, None) == "" for key in _FENCE_KEYS if key in environment}
    return {
        "compose_files": list(COMPOSE_FILES),
        "services": sorted(detail),
        "service_detail": detail,
        "fenced_empty_env_keys": {name: keys for name, keys in sorted(fence.items()) if keys},
        "volumes": {
            name: {"name": spec.get("name"), "external": bool(spec.get("external"))}
            for name, spec in sorted((document.get("volumes") or {}).items())
        },
        "networks": sorted(document.get("networks") or {}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="podman compose",
                        help="Compose invocation, e.g. 'docker compose' or 'podman compose'.")
    parser.add_argument("--check", action="store_true",
                        help="Compare against the committed snapshot instead of writing it.")
    args = parser.parse_args(argv)
    snapshot = json.dumps(render(shlex.split(args.engine)), indent=2, sort_keys=True) + "\n"
    if args.check:
        if SNAPSHOT_PATH.read_text(encoding="utf-8") != snapshot:
            sys.stderr.write("Rendered fleet topology does not match the committed snapshot.\n")
            return 1
        return 0
    SNAPSHOT_PATH.write_text(snapshot, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
