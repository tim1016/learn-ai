"""The generic fleet spine imports no provider implementation (PRD FR-005).

Static source scan plus an import-time check: every module under
``app/broker/fleet`` must be free of Alpaca (and IBKR) imports, and actually
importing the package must leave no provider execution module in
``sys.modules``. The Alpaca adapter lands as Phase 2 *beside* this package,
never inside it.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

FLEET_PACKAGE = Path(__file__).resolve().parents[3] / "app" / "broker" / "fleet"

FORBIDDEN_PREFIXES = (
    "app.broker.alpaca",
    "app.broker.ibkr",
    "app.routers.brokers",
    "app.routers.broker_v2_panel",
    "app.routers.broker_configuration",
    "app.routers.alpaca_clerk_sqlite",
)


def test_no_fleet_module_sources_import_a_provider_implementation() -> None:
    """No fleet module's source imports a provider implementation."""
    modules = sorted(FLEET_PACKAGE.glob("*.py"))
    assert modules, "the fleet package vanished"
    offenders: list[str] = []
    for module in modules:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                targets = [node.module]
            for target in targets:
                if target.startswith(FORBIDDEN_PREFIXES):
                    offenders.append(f"{module.name}: {target}")
    assert not offenders, (
        "generic fleet modules must not import provider implementations "
        f"(PRD FR-005): {offenders}"
    )


def test_importing_the_spine_loads_no_provider_execution_module() -> None:
    """A fresh interpreter imports the spine and nothing provider-shaped loads.

    Run in a subprocess deliberately: purging ``sys.modules`` in-process would
    mint a second fleet module object and silently flip every later test's
    exception-class identity.
    """
    probe = textwrap.dedent(
        """
        import json
        import sys

        import app.broker.fleet.service  # noqa: F401 - the import is the assertion

        loaded = [
            name
            for name in sys.modules
            if name.startswith(
                (
                    "app.broker.alpaca",
                    "app.broker.ibkr",
                    "app.routers.brokers",
                    "app.routers.broker_v2_panel",
                    "app.routers.broker_configuration",
                    "app.routers.alpaca_clerk_sqlite",
                )
            )
        ]
        print(json.dumps(loaded))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[3],
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [], result.stdout


def test_the_fleet_packages_own_registry_is_not_the_production_composition() -> None:
    """The fleet package's own adapter mapping stays empty; the real
    production registry is owned by ``fleet_composition``, not the package
    (PRD FR-001). ``production_adapter()`` — which resolved only against this
    deliberately-empty mapping, had no production caller, and always refused
    "alpaca" — is deleted (#2076); this pins its absence rather than its
    refusal, which a live "alpaca" registration would otherwise falsify."""
    import app.broker.fleet.provider as provider_module
    from app.broker.fleet.provider import PRODUCTION_PROVIDER_ADAPTERS
    from app.broker.fleet_composition import production_provider_adapters

    assert dict(PRODUCTION_PROVIDER_ADAPTERS) == {}
    assert set(production_provider_adapters()) == {"alpaca"}
    assert not hasattr(provider_module, "production_adapter")
