"""Contract: the always-on data plane builds the ``runtime`` stage (issue #2576).

``PythonDataService/Dockerfile`` ends with ``FROM runtime AS qualification``,
which installs ``requirements-dev.txt``. A build that names no ``target``
gets the *last* stage, so a compose service that says only
``build: {context: ./PythonDataService}`` quietly ships pytest and developer
tooling into an always-on trading process -- the opposite of what that
Dockerfile states. Nobody runs tests inside the data plane (the host venv is
the gate), so the fix is the build, not the comment.

Every compose service built from the ``PythonDataService`` context therefore
names its stage, and the one that legitimately wants the developer tooling
(the one-shot ``qualification`` profile) is the only one that may name
``qualification``. This reads each committed compose file per-file and
unmerged, exactly as the other compose contracts do: an overlay is reviewed
as written, and Compose merging can only add a ``target`` to a base file that
already names one.
"""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict

from tests.contracts.compose_files import ROOT, render_module, tracked_compose_files

DATA_PLANE_CONTEXT = "PythonDataService"
DOCKERFILE = ROOT / DATA_PLANE_CONTEXT / "Dockerfile"

# The only services allowed to build the developer-tooling stage: one-shot
# qualification runs whose test tree is mounted read-only.
QUALIFICATION_SERVICES = {("compose.yaml", "alpaca-clerk-qualification")}

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


def _data_plane_builds() -> dict[tuple[str, str], dict[str, str | None]]:
    """{(compose file, service): {"target": ..., "image": ...}} for every
    service, in any committed compose file, that builds the data-plane image."""
    load = render_module().load_compose_document
    builds: dict[tuple[str, str], dict[str, str | None]] = {}
    for name in tracked_compose_files():
        for service, spec in (load(ROOT / name).get("services") or {}).items():
            build = spec.get("build")
            if isinstance(build, str):
                build = {"context": build}
            if not build or posixpath.normpath(str(build.get("context", ""))) != DATA_PLANE_CONTEXT:
                continue
            builds[(name, service)] = {"target": build.get("target"), "image": spec.get("image")}
    return builds


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
    stage, which is the developer-tooling one."""
    wrong = {
        f"{name}:{service}": f"target={build['target']!r}, wants {expected!r}"
        for (name, service), build in _data_plane_builds().items()
        if build["target"] != (expected := "qualification" if (name, service) in QUALIFICATION_SERVICES else "runtime")
    }
    assert wrong == {}


def test_the_always_on_services_are_all_covered() -> None:
    assert set(_data_plane_builds()) >= ALWAYS_ON_SERVICES | QUALIFICATION_SERVICES


def test_two_stages_never_build_into_one_image_tag() -> None:
    """A shared tag lets whichever build ran last overwrite the other, so an
    always-on service could restart onto the qualification image without any
    compose file changing. Services that name no `image:` get a per-service
    default name and cannot collide."""
    targets_by_image: dict[str, set[str | None]] = defaultdict(set)
    for build in _data_plane_builds().values():
        if build["image"]:
            targets_by_image[str(build["image"])].add(build["target"])
    for image, targets in targets_by_image.items():
        assert len(targets) == 1, f"image tag {image!r} is built from more than one stage: {sorted(map(str, targets))}"


def test_the_runtime_stage_ships_no_developer_tooling() -> None:
    stages = _dockerfile_stages()
    for stage in ("builder", "runtime"):
        for line in stages[stage]:
            assert "requirements-dev" not in line, f"the {stage} stage installs developer requirements: {line!r}"
            assert "pytest" not in line, f"the {stage} stage ships pytest configuration: {line!r}"
    assert any("requirements-dev.txt" in line for line in stages["qualification"])
    assert stages["qualification"][0].split()[1] == "runtime"
