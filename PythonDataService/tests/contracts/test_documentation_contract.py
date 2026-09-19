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


def test_the_ibkr_authority_states_that_alpaca_execution_depends_on_this_feed() -> None:
    """#2077/#2080 both misread a live dependency as residue. The doc must say it.

    Three independent tokens can't fence the dependency (a reviewer could
    salt them into unrelated sentences), so the real fence is the section
    heading plus the verbatim topic sentence that names the relationship.
    """
    authority = (REPOSITORY_ROOT / "docs/ibkr-integration-authority.md").read_text(encoding="utf-8")
    assert "## Market data is load-bearing for Alpaca execution" in authority
    assert (
        "IBKR order actuation is retired, but the IBKR market-data feed is not residue "
        "— it is the live bar source Alpaca bots trade on."
    ) in authority
    assert "Alpaca" in authority
    assert "IbkrMarketDataFeed" in authority
    assert "IBKR_BROKER_ENABLED" in authority


def _write_served_pair(root: Path, canonical_text: str, served_text: str) -> tuple[str, str]:
    checker = _checker_module()
    canonical, served = next(iter(checker.SERVED_DOCUMENT_COPIES.items()))
    (root / canonical).parent.mkdir(parents=True, exist_ok=True)
    (root / served).parent.mkdir(parents=True, exist_ok=True)
    (root / canonical).write_text(canonical_text, encoding="utf-8")
    (root / served).write_text(served_text, encoding="utf-8")
    return canonical, served


def test_served_documents_fail_when_the_served_copy_drifts(tmp_path: Path) -> None:
    checker = _checker_module()
    _, served = _write_served_pair(tmp_path, "# Manual\n", "# Manual, edited in the app copy only\n")

    errors = checker._validate_served_documents(tmp_path)

    assert f"{served}: served copy differs from its canonical source" in " ".join(errors)


def test_served_documents_fail_when_a_github_link_names_a_missing_path(tmp_path: Path) -> None:
    checker = _checker_module()
    text = (
        "[ADR](https://github.com/tim1016/learn-ai/blob/master/docs/architecture/adrs/9999-gone.md)\n"
        "[Present](https://github.com/tim1016/learn-ai/blob/master/docs/present.md#section)\n"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/present.md").write_text("# Present\n", encoding="utf-8")
    _write_served_pair(tmp_path, text, text)

    errors = checker._validate_served_documents(tmp_path)

    assert any("docs/architecture/adrs/9999-gone.md" in error for error in errors)
    assert not any("docs/present.md" in error for error in errors)


def test_the_served_ibkr_guide_carries_no_retired_order_capable_guidance() -> None:
    checker = _checker_module()
    served = (REPOSITORY_ROOT / "Frontend/src/assets/docs/ibkr-setup-guide.md").read_text(encoding="utf-8")
    for retired in checker.FORBIDDEN_CURRENT_GUIDANCE:
        assert retired not in served
