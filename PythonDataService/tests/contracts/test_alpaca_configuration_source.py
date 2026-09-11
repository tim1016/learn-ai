"""Where an Alpaca configuration value is allowed to come from (ADR 0060).

Package D's assignment includes "remove non-secret environment reads from
migrated paths", and the property that actually protects a live worker is
narrower and checkable: **after this package, the process environment is read
only where a process bootstraps one, and every other consumer reads the
binding the worker resolved.**

That matters because the failure it prevents is silent. Two long-lived
``AlpacaBroker`` instances exist at runtime, both were settings-free, and both
deferred to a process-wide environment singleton — so a consumer that kept
reading the environment would keep answering for whatever ``.env`` says, which
after an applied profile switch is a *different account* than the one the
worker bound. Nothing would fail; the numbers would just be someone else's.

A grep-shaped invariant is the right instrument here for the same reason
`.claude/rules/temporal-rigor.md` states its ban list as one: the rule is
"nobody else may call this", which no single call site can assert about itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = _SERVICE_ROOT / "app"
SCRIPTS_ROOT = _SERVICE_ROOT / "scripts"

# The only modules that may read the process-wide environment settings, and
# why each one is admissible. Each is a *bootstrap*: a process resolving its
# own binding before one exists. None of them is a fallback for a binding that
# was attempted and refused — that raises.
ADMITTED_ENVIRONMENT_READERS: dict[str, str] = {
    "broker/alpaca/active_binding.py": (
        "the holder's own fallback for 'nothing has attempted a binding in this "
        "process yet' — a unit test constructing a consumer directly, or the "
        "pre-cutover bootstrap below. A *refused* binding raises here instead."
    ),
    "broker_configuration/worker_binding.py": (
        "the pre-cutover bootstrap: an installation with no saved profiles at "
        "all has no user-owned configuration to prefer. Package F's import "
        "retires this branch by writing the first profile."
    ),
    "broker_configuration/cli_binding.py": (
        "the same pre-cutover bootstrap, for the three operator CLIs. They run "
        "in their own processes where no binding has been installed, so they "
        "resolve one themselves; on a *configured* installation they refuse "
        "rather than fall back, exactly as the worker does."
    ),
}


def _modules_calling(function_name: str, root: Path = APP_ROOT) -> set[str]:
    """Every module under ``root`` that *calls* ``function_name``.

    Parsed rather than grepped so a mention in a docstring or a comment — of
    which there are several, deliberately, explaining this very rule — is not
    mistaken for a call.
    """
    callers: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if called == function_name:
                callers.add(path.relative_to(root).as_posix())
    return callers


def test_only_the_bootstrap_sites_read_the_process_environment() -> None:
    """Every other consumer reads the binding the worker resolved."""
    callers = _modules_calling("get_alpaca_settings")

    # The definition's own module is not a consumer of itself.
    callers.discard("broker/alpaca/config.py")

    assert callers == set(ADMITTED_ENVIRONMENT_READERS), (
        "A module started reading the Alpaca process environment directly. "
        "Read the resolved binding instead: "
        "app.broker.alpaca.active_binding.resolved_alpaca_settings(). "
        f"Unexpected: {sorted(callers - set(ADMITTED_ENVIRONMENT_READERS))}; "
        f"missing: {sorted(set(ADMITTED_ENVIRONMENT_READERS) - callers)}"
    )


def test_the_admitted_readers_still_exist_and_are_documented() -> None:
    """A stale allowlist entry is as bad as a missing one."""
    for module, reason in ADMITTED_ENVIRONMENT_READERS.items():
        assert (APP_ROOT / module).is_file(), f"{module} no longer exists"
        assert reason.strip(), f"{module} is admitted without a stated reason"


def test_no_operator_script_reads_the_process_environment() -> None:
    """The rule covers ``scripts/`` too, which is how the last one slipped through.

    Package D migrated the three ``manage_alpaca_*`` CLIs to ``cli_binding`` but
    the sweep above only walked ``app/``, so ``hitl_alpaca_capture.py`` kept
    calling ``get_alpaca_settings`` directly and kept passing CI. An operator
    script that reads ``.env`` on a cut-over installation answers for whatever
    that file says — which after an applied profile switch is a different
    account than the worker bound.

    ``scripts/`` has no admitted readers at all: every one of these runs in its
    own process and resolves through ``cli_binding``, which does the bootstrap
    for them.
    """
    callers = _modules_calling("get_alpaca_settings", SCRIPTS_ROOT)

    assert callers == set(), (
        "An operator script started reading the Alpaca process environment "
        "directly. Resolve through "
        "app.broker_configuration.cli_binding.effective_alpaca_settings() "
        f"instead. Unexpected: {sorted(callers)}"
    )
