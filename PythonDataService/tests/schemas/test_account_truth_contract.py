import re
from pathlib import Path
from typing import get_args

from app.schemas.account_truth import AccountTruthExecutionUncertaintyCode

REPO_ROOT = Path(__file__).resolve().parents[3]
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


def _typescript_string_union_values(path: Path, type_name: str) -> list[str]:
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"export type {re.escape(type_name)}\s*=\s*(.*?);", text, re.S)
    assert match is not None, f"{type_name} was not found in {path}"
    values = re.findall(r"'([^']+)'", match.group(1))
    assert values, f"{type_name} in {path} did not contain string literal members"
    assert len(values) == len(set(values)), f"{type_name} in {path} contains duplicate members"
    return values
