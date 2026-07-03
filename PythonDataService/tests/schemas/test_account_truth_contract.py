import re
from pathlib import Path
from typing import get_args

import pytest

from app.schemas.account_truth import (
    AccountTruthExecutionUncertaintyCode,
    AccountTruthSourceFreshnessStatus,
    AccountTruthSourceName,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_ROOT = REPO_ROOT / "Frontend"
BROKER_MODELS_PATH = REPO_ROOT / "Frontend" / "src" / "app" / "api" / "broker-models.ts"


def test_execution_uncertainty_code_typescript_mirror_matches_python_literal() -> None:
    """The hand-mirrored frontend union must stay locked to Python Account Truth."""
    python_codes = list(get_args(AccountTruthExecutionUncertaintyCode))
    typescript_codes = _typescript_string_union_values(
        BROKER_MODELS_PATH,
        "AccountTruthExecutionUncertaintyCode",
    )

    assert typescript_codes == python_codes, (
        "AccountTruthExecutionUncertaintyCode drifted between Python and Frontend.\n"
        f"Python literal: {python_codes}\n"
        f"TypeScript union: {typescript_codes}\n"
        f"Missing from TypeScript: {sorted(set(python_codes) - set(typescript_codes))}\n"
        f"Extra in TypeScript: {sorted(set(typescript_codes) - set(python_codes))}"
    )


@pytest.mark.parametrize(
    ("python_literal", "typescript_type"),
    [
        (AccountTruthSourceName, "AccountTruthSourceName"),
        (AccountTruthSourceFreshnessStatus, "AccountTruthSourceFreshnessStatus"),
    ],
)
def test_source_freshness_typescript_mirrors_match_python_literals(
    python_literal,
    typescript_type: str,
) -> None:
    """Source freshness is part of the Account Truth wire contract."""
    python_codes = list(get_args(python_literal))
    typescript_codes = _typescript_string_union_values(
        BROKER_MODELS_PATH,
        typescript_type,
    )

    assert typescript_codes == python_codes, (
        f"{typescript_type} drifted between Python and Frontend.\n"
        f"Python literal: {python_codes}\n"
        f"TypeScript union: {typescript_codes}\n"
        f"Missing from TypeScript: {sorted(set(python_codes) - set(typescript_codes))}\n"
        f"Extra in TypeScript: {sorted(set(typescript_codes) - set(python_codes))}"
    )


def _typescript_string_union_values(path: Path, type_name: str) -> list[str]:
    if not FRONTEND_ROOT.exists():
        pytest.skip("Frontend tree is not mounted; skipping cross-stack mirror contract")
    assert path.exists(), f"{type_name} mirror file was not found at {path}"

    text = path.read_text(encoding="utf-8")
    match = re.search(rf"export type {re.escape(type_name)}\s*=\s*(.*?);", text, re.S)
    assert match is not None, f"{type_name} was not found in {path}"
    alias_body = _strip_typescript_comments(match.group(1))
    values = re.findall(r"'([^']+)'", alias_body)
    assert values, f"{type_name} in {path} did not contain string literal members"
    assert len(values) == len(set(values)), f"{type_name} in {path} contains duplicate members"
    return values


def _strip_typescript_comments(text: str) -> str:
    without_blocks = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//.*", "", without_blocks)
