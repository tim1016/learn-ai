"""Validate the repository's live documentation and Codex/Claude boundary."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
ADR_INDEX_ROW = re.compile(r"^\|\s*(\d{4})\s*\|", re.MULTILINE)
ANGULAR_VERSION = re.compile(r"Angular\s+(\d+)")
DOTNET_VERSION = re.compile(r"\.NET\s+(\d+)")
TARGET_FRAMEWORK = re.compile(r"<TargetFramework>net(\d+)\.\d+</TargetFramework>")

CLAUDE_COMPATIBILITY_PATHS = (
    "CLAUDE.md",
    ".claude/CLAUDE.md",
    ".claude/rules",
    ".claude/skills",
    ".claude/settings.json",
    ".claude/commands",
    ".claude/hooks",
)

EXACT_DOCUMENT_CLASSES = {
    "docs/CURRENT.md": "protected-canonical",
    "docs/agent-start-here.md": "protected-canonical",
    "docs/architecture/engine-authority-map.md": "protected-canonical",
    "docs/architecture/numerical-authority-migration-plan.md": "protected-canonical",
    "docs/broker-v2-operator-manual.md": "canonical",
    "docs/doc-authority.md": "canonical",
    "docs/ibkr-integration-authority.md": "canonical",
    "docs/known-gaps.md": "canonical",
    "docs/math-sources-of-truth.md": "protected-canonical",
}

RETIRED_DOCUMENTS = (
    "docs/arch-overview.md",
    "docs/bot-control-operator-manual.md",
    "docs/codex-phase-1-4-audit.md",
    "docs/architecture/account-clerk-callback-cursors.md",
    "docs/architecture/offline-market-replay.md",
    "docs/audits/state-writer-census-2026-08-17.md",
)

FORBIDDEN_CURRENT_GUIDANCE = (
    "IBKR_READONLY=false",
    "docs/bot-control-operator-manual.md",
    "launch_eight_bot_paper_run.py",
)

ROOT_RELATIVE_LINK_PREFIXES = (
    ".claude/",
    "Backend/",
    "Frontend/",
    "PythonDataService/",
    "contracts/",
    "docs/",
    "references/",
)


def _markdown_files(root: Path) -> list[Path]:
    return sorted((root / "docs").rglob("*.md"))


def classify_document(root: Path, path: Path) -> str | None:
    relative = path.relative_to(root).as_posix()
    exact = EXACT_DOCUMENT_CLASSES.get(relative)
    if exact is not None:
        return exact

    parts = Path(relative).parts
    if len(parts) == 2:
        return "supporting"
    if len(parts) < 3 or parts[0] != "docs":
        return None

    directory = parts[1]
    if directory == "archive":
        return "archived"
    if directory == "architecture" and len(parts) >= 4 and parts[2] == "adrs":
        return "canonical"
    if directory == "prds":
        return "in-flight"
    if directory in {
        "architecture",
        "audits",
        "design",
        "domain",
        "process",
        "references",
        "runbooks",
        "screenshots",
        "spy-lean-output",
        "superpowers",
        "validation",
    }:
        return "supporting"
    return None


def _local_markdown_links(path: Path) -> list[str]:
    targets: list[str] = []
    for raw_target in MARKDOWN_LINK.findall(path.read_text(encoding="utf-8")):
        target = raw_target.strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        target = target.split(maxsplit=1)[0].split("#", maxsplit=1)[0]
        target = target.split("?", maxsplit=1)[0]
        if not target or target.startswith(("#", "/", "http://", "https://", "mailto:")):
            continue
        if re.match(r"^[A-Za-z]:[/\\]", target):
            continue
        if any(character in target for character in "{}*"):
            continue
        targets.append(target)
    return targets


def _validate_local_links(root: Path) -> list[str]:
    errors: list[str] = []
    resolved_root = root.resolve()
    for source in _markdown_files(root):
        relative_source = source.relative_to(root)
        classification = classify_document(root, source)
        is_current_runbook = relative_source.parts[:2] == ("docs", "runbooks")
        if classification not in {"canonical", "protected-canonical", "in-flight"} and not is_current_runbook:
            continue
        for target in _local_markdown_links(source):
            destination = (
                root / target
                if target.startswith(ROOT_RELATIVE_LINK_PREFIXES)
                else source.parent / target
            ).resolve()
            try:
                destination.relative_to(resolved_root)
            except ValueError:
                errors.append(
                    f"{source.relative_to(root)}: link escapes repository: {target}"
                )
                continue
            if not destination.exists():
                errors.append(
                    f"{source.relative_to(root)}: missing local link: {target}"
                )
    return errors


def _validate_claude_compatibility(root: Path) -> list[str]:
    errors: list[str] = []
    agents_path = root / "AGENTS.md"
    if not agents_path.exists():
        return ["AGENTS.md is missing"]

    agents = agents_path.read_text(encoding="utf-8")
    if "## Codex and Claude compatibility" not in agents:
        errors.append("AGENTS.md: missing Codex/Claude compatibility fence")
    if re.search(
        r"AGENTS\.md.{0,120}\b(?:supersedes|replaces|overrides)\b.{0,120}CLAUDE",
        agents,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        errors.append("AGENTS.md: claims to supersede the Claude hierarchy")

    for relative in CLAUDE_COMPATIBILITY_PATHS:
        if not (root / relative).exists():
            errors.append(f"Claude compatibility path is missing: {relative}")
    return errors


def _validate_document_classification(root: Path) -> list[str]:
    return [
        f"unclassified live document: {path.relative_to(root)}"
        for path in _markdown_files(root)
        if classify_document(root, path) is None
    ]


def _validate_adr_index(root: Path) -> list[str]:
    index_path = root / "docs/doc-authority.md"
    if not index_path.exists():
        return ["docs/doc-authority.md is missing"]

    indexed = ADR_INDEX_ROW.findall(index_path.read_text(encoding="utf-8"))
    actual = [path.name[:4] for path in sorted((root / "docs/architecture/adrs").glob("*.md"))]
    errors: list[str] = []
    duplicates = sorted({adr for adr in indexed if indexed.count(adr) > 1})
    if duplicates:
        errors.append(f"ADR index has duplicate rows: {', '.join(duplicates)}")
    if set(indexed) != set(actual):
        missing = sorted(set(actual) - set(indexed))
        extra = sorted(set(indexed) - set(actual))
        if missing:
            errors.append(f"ADR index is missing: {', '.join(missing)}")
        if extra:
            errors.append(f"ADR index has no matching file: {', '.join(extra)}")
    return errors


def _major_from_version(value: str) -> str:
    match = re.search(r"(\d+)", value)
    if match is None:
        raise ValueError(f"no major version in {value!r}")
    return match.group(1)


def _validate_manifest_versions(root: Path) -> list[str]:
    errors: list[str] = []
    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    package = json.loads((root / "Frontend/package.json").read_text(encoding="utf-8"))
    angular_major = _major_from_version(package["dependencies"]["@angular/core"])
    target_framework = (root / "Backend/Backend.csproj").read_text(encoding="utf-8")
    framework_match = TARGET_FRAMEWORK.search(target_framework)
    if framework_match is None:
        return ["Backend/Backend.csproj: TargetFramework was not found"]
    dotnet_major = framework_match.group(1)

    for claimed in ANGULAR_VERSION.findall(agents):
        if claimed != angular_major:
            errors.append(
                f"AGENTS.md: Angular {claimed} disagrees with Frontend/package.json ({angular_major})"
            )
    for claimed in DOTNET_VERSION.findall(agents):
        if claimed != dotnet_major:
            errors.append(
                f"AGENTS.md: .NET {claimed} disagrees with Backend.csproj ({dotnet_major})"
            )
    return errors


def _validate_retired_documentation(root: Path) -> list[str]:
    errors: list[str] = []
    for relative in RETIRED_DOCUMENTS:
        if (root / relative).exists():
            errors.append(f"retired document still exists: {relative}")

    for path in _markdown_files(root):
        if path.is_relative_to(root / "docs/archive"):
            continue
        text = path.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_CURRENT_GUIDANCE:
            if forbidden in text:
                errors.append(f"{path.relative_to(root)}: retired guidance: {forbidden}")
    return errors


def validate_repository(root: Path) -> list[str]:
    """Return every deterministic documentation-contract violation."""

    return [
        *_validate_claude_compatibility(root),
        *_validate_document_classification(root),
        *_validate_local_links(root),
        *_validate_adr_index(root),
        *_validate_manifest_versions(root),
        *_validate_retired_documentation(root),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()

    errors = validate_repository(args.root.resolve())
    if not errors:
        return 0
    for error in errors:
        print(error, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
