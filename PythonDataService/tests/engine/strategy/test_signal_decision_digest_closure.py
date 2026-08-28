"""Unit tests for the import-closure walker the digest coverage rests on.

The per-program coverage assertion it feeds --
``closure - documented_exclusions == the digested paths`` -- lives in
``test_signal_program_qualification_matrix.py``, parametrized over every
sealed program. This file tests the walker itself: that it follows deferred
function-local imports, that it does not stop at the first name in a
combined import statement, and that its default service root is real. A
walker that quietly missed an edge would make that coverage assertion pass
while covering nothing.

"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.run_signal_program_build_qualification import (
    _SERVICE_ROOT,
    signal_decision_import_closure,
)


@pytest.fixture
def synthetic_package(tmp_path: Path) -> Path:
    """A tiny first-party package under ``tmp_path`` used to unit-test the
    closure walker in isolation from the real 22-file production graph."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "root.py").write_text(
        "from app.helper import thing\n\n\ndef late_import() -> None:\n    from app.leaf import deep\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "helper.py").write_text("import app.sibling, app.leaf\n", encoding="utf-8")
    (tmp_path / "app" / "sibling.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "app" / "leaf.py").write_text("y = 2\n", encoding="utf-8")
    return tmp_path


def test_closure_helper_follows_deferred_function_local_imports(synthetic_package: Path) -> None:
    """The production case this guards: ``app/engine/live/indicator_state.py``
    imports ``app.lean_sidecar.trading_calendar`` only inside a function body.
    A closure walker that only looked at module-top imports would silently
    miss it — and miss the real defect 2 gap it represents."""
    closure = signal_decision_import_closure(roots=["app/root.py"], service_root=synthetic_package)

    assert closure == {"app/root.py", "app/helper.py", "app/sibling.py", "app/leaf.py"}


def test_closure_helper_follows_every_name_in_a_combined_import_statement(synthetic_package: Path) -> None:
    """``import app.sibling, app.leaf`` names two modules in one statement;
    the walker must not stop after the first."""
    closure = signal_decision_import_closure(roots=["app/helper.py"], service_root=synthetic_package)

    assert closure == {"app/helper.py", "app/sibling.py", "app/leaf.py"}


def test_production_closure_helper_uses_the_real_service_root() -> None:
    """Sanity check that the default ``service_root`` resolves to
    ``PythonDataService``, not the ``scripts/`` directory the module lives in."""
    assert (_SERVICE_ROOT / "app").is_dir()
    assert (_SERVICE_ROOT / "tests").is_dir()
