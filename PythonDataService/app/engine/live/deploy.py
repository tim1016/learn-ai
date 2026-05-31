"""Shared run-creation (deploy) logic for the live runtime — ADR 0006.

Extracted from the ``init-ledger`` CLI so both the CLI and the host daemon's
``POST /deploy`` endpoint create runs through one path. ``deploy_run`` performs
the dirty-tree gate, captures git HEAD as ``code_sha``, builds and writes the
ledger, and resolves idempotency on the content-addressed ``run_id``.

It raises typed exceptions and never calls ``print`` / ``sys.exit`` — callers
map the exceptions to CLI exit codes (``run.cmd_init_ledger``) or HTTP statuses
(``host_daemon.RunnerProcessManager.deploy``).

Idempotency: ``run_id`` is a pure function of (committed code + inputs), so a
re-deploy of identical inputs against the same HEAD recomputes the same
``run_id``. With ``idempotent=True`` an existing run directory whose ledger
matches is returned as a no-op (``created=False``); a directory that exists
without a matching ledger is a genuine collision (``RunAlreadyExistsError``).
The CLI keeps the non-idempotent contract (existing dir -> error) so its exit
codes are unchanged.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from app.engine.live.pre_flight import check_clean_tree
from app.engine.live.run_ledger import (
    LiveRunLedger,
    build_ledger,
    read_ledger,
    write_ledger,
)

# Default paths included in the dirty-tree gate. Mirrors the CLI default so the
# CLI and the daemon refuse to deploy from the same dirty scope.
DEFAULT_CLEAN_TREE_SCOPE: tuple[str, ...] = ("PythonDataService", "references/qc-shadow")


class DeployError(Exception):
    """Base for deploy failures that map to a CLI exit code or HTTP status."""


class DirtyTreeError(DeployError):
    """The git working tree is dirty within the clean-tree scope (halt)."""


class GitUnavailableError(DeployError):
    """git HEAD could not be resolved (git missing, not a repo, empty HEAD)."""


class SpecOrAuditMissingError(DeployError):
    """The strategy spec or QC audit-copy path does not exist on disk."""


class RunAlreadyExistsError(DeployError):
    """The content-addressed run directory already exists.

    Raised for a non-idempotent caller, or (idempotent caller) when the
    directory exists but holds no ledger matching the recomputed ``run_id`` —
    a genuine collision rather than a safe re-deploy.
    """

    def __init__(self, run_id: str, run_dir: Path) -> None:
        super().__init__(f"run directory already exists: {run_dir}")
        self.run_id = run_id
        self.run_dir = run_dir


@dataclass(frozen=True)
class DeployParams:
    """Inputs to a single run creation. ``repo_root`` / ``run_root`` are owned
    by the caller (the daemon supplies its own; the CLI takes them as args) —
    never client-chosen on the daemon path."""

    repo_root: Path
    strategy_spec_path: Path
    qc_audit_copy_path: Path
    qc_cloud_backtest_id: str
    account_id: str
    start_date_ms: int
    run_root: Path
    live_config: dict = field(default_factory=dict)
    strategy_instance_id: str = ""
    clean_tree_scope: tuple[str, ...] = DEFAULT_CLEAN_TREE_SCOPE
    force: bool = False
    idempotent: bool = False


@dataclass(frozen=True)
class DeployResult:
    run_id: str
    run_dir: Path
    created: bool
    ledger: LiveRunLedger


def git_head_sha(repo_root: Path) -> str:
    """Resolve git HEAD as the run's ``code_sha``.

    The dirty-tree gate in :func:`deploy_run` is what makes this the *actual*
    identity of the running code rather than a "close enough" hint.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitUnavailableError(f"git rev-parse HEAD failed in {repo_root}: {exc}") from exc
    if proc.returncode != 0:
        raise GitUnavailableError(
            f"git rev-parse HEAD failed in {repo_root}: rc={proc.returncode} stderr={proc.stderr!r}"
        )
    sha = proc.stdout.strip()
    if not sha:
        raise GitUnavailableError(f"git rev-parse HEAD returned empty in {repo_root}")
    return sha


def deploy_run(params: DeployParams) -> DeployResult:
    """Create a live run: dirty-tree gate -> code_sha -> ledger -> write.

    Synchronous (subprocess + filesystem). Daemon callers run it in a
    threadpool. Raises a :class:`DeployError` subclass on every failure.
    """
    repo_root = params.repo_root.resolve()
    scope_paths = [Path(p) for p in params.clean_tree_scope]

    clean = check_clean_tree(scope_paths, repo_root=repo_root)
    if not clean.passed:
        raise DirtyTreeError(clean.detail)

    code_sha = git_head_sha(repo_root)

    try:
        ledger = build_ledger(
            code_sha=code_sha,
            strategy_spec_path=params.strategy_spec_path,
            qc_audit_copy_path=params.qc_audit_copy_path,
            qc_cloud_backtest_id=params.qc_cloud_backtest_id,
            account_id=params.account_id,
            start_date_ms=params.start_date_ms,
            live_config=params.live_config,
            strategy_instance_id=params.strategy_instance_id,
        )
    except FileNotFoundError as exc:
        raise SpecOrAuditMissingError(str(exc)) from exc

    run_dir = params.run_root / ledger.run_id
    ledger_path = run_dir / "run_ledger.json"

    if run_dir.exists() and not params.force:
        if params.idempotent and ledger_path.is_file():
            try:
                existing = read_ledger(ledger_path)
            except (OSError, ValueError) as exc:
                raise RunAlreadyExistsError(ledger.run_id, run_dir) from exc
            if existing.run_id == ledger.run_id:
                return DeployResult(
                    run_id=ledger.run_id, run_dir=run_dir, created=False, ledger=existing
                )
            raise RunAlreadyExistsError(ledger.run_id, run_dir)
        raise RunAlreadyExistsError(ledger.run_id, run_dir)

    write_ledger(ledger_path, ledger)
    return DeployResult(run_id=ledger.run_id, run_dir=run_dir, created=True, ledger=ledger)
