"""Contract: the always-on data plane builds the ``runtime`` stage (issue #2576).

A build that names no ``target`` gets the *last* stage of
``PythonDataService/Dockerfile``, so a stage added after ``runtime`` would ship
into every always-on trading process that left its target unnamed. Nobody runs
tests inside the data plane (the host venv is the gate), so the runtime stage
ships no developer tooling, and nothing in it reads a requirement file (the
sweep identity digests the installed distributions, #2588).

Every compose service built from the ``PythonDataService`` context therefore
names ``runtime``. Each committed compose file is read per-file and unmerged,
exactly as the other compose contracts do. Compose merging lets an overlay
*override* a base file's ``target``, so an overlay ``build:`` block that
carries no ``context`` of its own is still checked, for any service some
tracked file builds from the data-plane context.
"""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass

from tests.contracts.compose_files import ROOT, render_module, tracked_compose_files

DATA_PLANE_CONTEXT = "PythonDataService"
DOCKERFILE = ROOT / DATA_PLANE_CONTEXT / "Dockerfile"

# Every always-on process built from this context. Named so a sweep that
# stopped matching anything -- a renamed service, a moved build block -- fails
# instead of passing vacuously.
ALWAYS_ON_SERVICES = {
    ("compose.yaml", "python-service"),
    ("compose.fleet.yaml", "fleet-coordinator"),
    ("compose.fleet.yaml", "alpaca-paper-clerk"),
    ("compose.fleet.yaml", "alpaca-live-clerk"),
    ("compose.fleet.dev.yaml", "alpaca-paper-clerk"),
    ("compose.fleet.dev.yaml", "alpaca-live-clerk"),
}

_STAGE_HEADER = re.compile(r"^FROM\s+\S+(?:\s+AS\s+(?P<name>\S+))?\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class Build:
    has_context: bool
    target: str | None
    image: str | None


def _load_documents() -> dict[str, dict[str, object]]:
    load = render_module().load_compose_document
    return {name: load(ROOT / name) for name in tracked_compose_files()}


def _data_plane_builds(documents: dict[str, dict[str, object]]) -> dict[tuple[str, str], Build]:
    """{(compose file, service): Build} for every service that builds the
    data-plane image: one whose own `build:` names the context, plus any
    overlay `build:` block for a service another file builds from it."""
    declared: dict[tuple[str, str], tuple[dict[str, object], dict[str, object]]] = {}
    data_plane_services: set[str] = set()
    for name, document in documents.items():
        for service, spec in (document.get("services") or {}).items():
            build = spec.get("build")
            if isinstance(build, str):
                build = {"context": build}
            if not build:
                continue
            declared[(name, service)] = (build, spec)
            if posixpath.normpath(str(build.get("context", ""))) == DATA_PLANE_CONTEXT:
                data_plane_services.add(service)
    return {
        key: Build(has_context="context" in build, target=build.get("target"), image=spec.get("image"))
        for key, (build, spec) in declared.items()
        if key[1] in data_plane_services
    }


def _wrong_stages(builds: dict[tuple[str, str], Build]) -> dict[str, str]:
    """Each service whose named stage is not the one it may build. A build
    block with a context must name the stage; an overlay block without one may
    stay silent (Compose keeps the base file's target) but never override it."""
    wrong: dict[str, str] = {}
    for (name, service), build in builds.items():
        if build.target == "runtime" or (build.target is None and not build.has_context):
            continue
        wrong[f"{name}:{service}"] = f"target={build.target!r}, wants 'runtime'"
    return wrong


def _dockerfile_stages() -> dict[str, list[str]]:
    """{stage name: its instruction lines, comments dropped}."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    headers = list(_STAGE_HEADER.finditer(text))
    stages: dict[str, list[str]] = {}
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        lines = text[header.start() : end].splitlines()
        stages[header.group("name")] = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    return stages


def test_every_service_built_from_the_data_plane_context_names_its_stage() -> None:
    """No `target:` is the trap itself: Docker builds the last Dockerfile
    stage, whatever stage that is."""
    assert _wrong_stages(_data_plane_builds(_load_documents())) == {}


def test_an_overlay_cannot_retarget_a_data_plane_service_without_a_context() -> None:
    """A base file names the stage; an overlay `build: {target: ...}` with no
    context of its own would silently override it."""
    documents: dict[str, dict[str, object]] = {
        "compose.yaml": {"services": {"svc": {"build": {"context": "./PythonDataService", "target": "runtime"}}}},
        "overlay.yaml": {"services": {"svc": {"build": {"target": "qualification"}}}},
    }

    builds = _data_plane_builds(documents)

    assert set(builds) == {("compose.yaml", "svc"), ("overlay.yaml", "svc")}
    assert _wrong_stages(builds) == {"overlay.yaml:svc": "target='qualification', wants 'runtime'"}


def test_the_always_on_services_are_all_covered() -> None:
    assert set(_data_plane_builds(_load_documents())) >= ALWAYS_ON_SERVICES


def test_two_stages_never_build_into_one_image_tag() -> None:
    """A shared tag lets whichever build ran last overwrite the other, so an
    always-on service could restart onto another stage's image without any
    compose file changing. Services that name no `image:` get a per-service
    default name and cannot collide."""
    targets_by_image: dict[str, set[str | None]] = defaultdict(set)
    for build in _data_plane_builds(_load_documents()).values():
        if build.image:
            targets_by_image[build.image].add(build.target)
    for image, targets in targets_by_image.items():
        assert len(targets) == 1, f"image tag {image!r} is built from more than one stage: {sorted(map(str, targets))}"


def test_the_runtime_stage_ships_no_developer_tooling() -> None:
    stages = _dockerfile_stages()
    for stage in ("builder", "runtime"):
        for line in stages[stage]:
            assert "requirements-dev" not in line, f"the {stage} stage installs developer requirements: {line!r}"
            assert "pytest" not in line, f"the {stage} stage ships pytest configuration: {line!r}"
            assert "ruff.toml" not in line, f"the {stage} stage ships ruff configuration: {line!r}"


def test_the_builder_pins_its_installer_exactly() -> None:
    """An unpinned `pip install --upgrade pip` moves the installed pip on every
    cache-skipping rebuild, which moves the sweep environment digest and
    refuses Finish on every interrupted study (#2604)."""
    stages = _dockerfile_stages()
    upgrade = next(line for line in stages["builder"] if "pip install" in line and ("--upgrade" in line or "==" in line))

    assert "pip==" in upgrade, f"pip is not pinned to an exact version: {upgrade!r}"
    assert "setuptools==" in upgrade, f"setuptools is not pinned to an exact version: {upgrade!r}"
