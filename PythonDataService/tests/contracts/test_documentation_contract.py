from __future__ import annotations

import importlib.util
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CHECKER_PATH = REPOSITORY_ROOT / "scripts" / "check_documentation_contract.py"


def _checker_module() -> object:
    spec = importlib.util.spec_from_file_location("documentation_contract", CHECKER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("documentation contract checker could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_fixture(root: Path) -> None:
    (root / "docs/architecture/adrs").mkdir(parents=True)
    (root / ".claude/rules").mkdir(parents=True)
    (root / ".claude/skills").mkdir(parents=True)
    (root / ".claude/commands").mkdir(parents=True)
    (root / ".claude/hooks").mkdir(parents=True)
    (root / "Frontend").mkdir()
    (root / "Backend").mkdir()

    (root / "AGENTS.md").write_text(
        "# Agent\n\n## Codex and Claude compatibility\n\nCodex is additive.\n\nAngular 22\n.NET 10\n",
        encoding="utf-8",
    )
    (root / "CLAUDE.md").write_text("# Claude\n", encoding="utf-8")
    (root / ".claude/CLAUDE.md").write_text("# Claude local\n", encoding="utf-8")
    (root / ".claude/settings.json").write_text("{}\n", encoding="utf-8")
    (root / "Frontend/package.json").write_text(
        '{"dependencies":{"@angular/core":"^22.0.0"}}\n', encoding="utf-8"
    )
    (root / "Backend/Backend.csproj").write_text(
        "<Project><PropertyGroup><TargetFramework>net10.0</TargetFramework></PropertyGroup></Project>\n",
        encoding="utf-8",
    )
    (root / "docs/doc-authority.md").write_text(
        "# Docs\n\n| ADR | Decision |\n|---|---|\n| 0001 | Fixture |\n",
        encoding="utf-8",
    )
    (root / "docs/architecture/adrs/0001-fixture.md").write_text(
        "# ADR 0001\n", encoding="utf-8"
    )


def test_validate_repository_current_repository_passes() -> None:
    checker = _checker_module()

    assert checker.validate_repository(REPOSITORY_ROOT) == []


def test_research_documents_are_supporting_evidence(tmp_path: Path) -> None:
    checker = _checker_module()
    path = tmp_path / "docs/research/fixture.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Research evidence\n", encoding="utf-8")

    assert checker.classify_document(tmp_path, path) == "supporting"


def test_validate_repository_reports_independent_contract_failures(tmp_path: Path) -> None:
    checker = _checker_module()
    _write_fixture(tmp_path)
    (tmp_path / "docs/unclassified").mkdir()
    (tmp_path / "docs/unclassified/note.md").write_text("# Unknown\n", encoding="utf-8")
    (tmp_path / "docs/runbooks").mkdir()
    (tmp_path / "docs/runbooks/broken.md").write_text(
        "[missing](missing.md)\n", encoding="utf-8"
    )
    (tmp_path / "docs/runbooks/ibkr.md").write_text(
        "IBKR_READONLY=false\n", encoding="utf-8"
    )
    (tmp_path / "AGENTS.md").write_text(
        "## Codex and Claude compatibility\nAGENTS.md supersedes CLAUDE.\nAngular 99\n.NET 10\n",
        encoding="utf-8",
    )
    (tmp_path / "docs/architecture/adrs/0002-unindexed.md").write_text(
        "# ADR 0002\n", encoding="utf-8"
    )
    (tmp_path / "docs/architecture/offline-market-replay.md").write_text(
        "# Retired\n", encoding="utf-8"
    )

    errors = checker.validate_repository(tmp_path)

    assert any("supersede the Claude hierarchy" in error for error in errors)
    assert any("unclassified live document" in error for error in errors)
    assert any("missing local link" in error for error in errors)
    assert any("retired guidance: IBKR_READONLY=false" in error for error in errors)
    assert any("ADR index is missing: 0002" in error for error in errors)
    assert any("Angular 99" in error for error in errors)
    assert any("retired document still exists" in error for error in errors)
