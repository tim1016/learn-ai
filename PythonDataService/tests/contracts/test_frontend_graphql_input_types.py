"""Every GraphQL variable type the Frontend hand-writes must exist in the .NET schema snapshot.

The Angular services post raw ``{ query, variables }`` documents to the .NET
GraphQL API; there is no codegen since #1970, so a variable declared as
``$input: DataLabSessionInputInput!`` is only rejected at runtime, when Hot
Chocolate answers 400 "not compatible with the type of the current location"
(#1971, #1972). This test reads those declarations out of the TypeScript
sources and checks each named type against ``contracts/graphql/backend.schema.graphql``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_ROOT = REPOSITORY_ROOT / "Frontend/src/app"
SCHEMA_PATH = REPOSITORY_ROOT / "contracts/graphql/backend.schema.graphql"

# Built-in GraphQL scalars; custom scalars come from the schema itself.
BUILTIN_SCALARS = frozenset({"Int", "Float", "String", "Boolean", "ID"})

_OPERATION = re.compile(r"\b(?:query|mutation)\s+[A-Za-z_]\w*\s*\(")
# ``$name: Type``, ``$name: Type!``, ``$name: [Type!]!`` — the named type is captured.
_VARIABLE = re.compile(r"\$[A-Za-z_]\w*\s*:\s*\[?\s*([A-Za-z_]\w*)")
_DECLARATION = re.compile(r"^(?:input|enum|scalar)\s+([A-Za-z_]\w*)", re.MULTILINE)


def _schema_variable_types() -> frozenset[str]:
    return frozenset(_DECLARATION.findall(SCHEMA_PATH.read_text(encoding="utf-8"))) | BUILTIN_SCALARS


def _frontend_variable_types() -> dict[str, set[str]]:
    """``{type name: {"path:line", ...}}`` for every variable declared in a GraphQL operation."""
    found: dict[str, set[str]] = {}
    for path in sorted(FRONTEND_ROOT.rglob("*.ts")):
        if path.name.endswith(".spec.ts"):
            continue
        text = path.read_text(encoding="utf-8")
        if not _OPERATION.search(text):
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            for type_name in _VARIABLE.findall(line):
                found.setdefault(type_name, set()).add(f"{path.relative_to(REPOSITORY_ROOT)}:{line_number}")
    return found


def test_frontend_graphql_variable_types_exist_in_schema() -> None:
    declared = _schema_variable_types()
    used = _frontend_variable_types()
    assert used, "no GraphQL operations found under Frontend/src/app; the scan is broken"
    unknown = {name: sorted(sites) for name, sites in used.items() if name not in declared}
    assert not unknown, (
        "GraphQL variable types not declared in contracts/graphql/backend.schema.graphql "
        f"(Hot Chocolate rejects these with 400 at runtime): {unknown}"
    )


def test_scan_sees_the_known_input_types() -> None:
    """The scan must reach the two services #1971 and #1972 repaired, or a regression would pass silently."""
    used = _frontend_variable_types()
    assert "DataLabSessionInput" in used
    assert "PriceInput" in used
