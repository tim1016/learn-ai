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

from app.engine.live.identity import validate_strategy_instance_id
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


class InvalidInstanceIdError(DeployError):
    """The ``strategy_instance_id`` is not a safe, operable single-segment id.

    The operate endpoints (``status`` / ``start`` / ``stop``) enforce a strict
    single-segment pattern; a name they would reject (e.g. one containing a
    space) must be rejected at *creation* too, or it yields a run that exists
    but can never be selected or started. Maps to HTTP 400.
    """


class StrategyInstanceIdAlreadyUsedError(DeployError):
    """The bot name / strategy instance id has historical evidence already.

    ``strategy_instance_id`` is the durable Bot Cockpit identity and broker
    attribution namespace. A stopped or retired bot name cannot be reused for a
    new run because old paths and ``order_ref`` evidence remain authoritative.
    """

    def __init__(self, strategy_instance_id: str, existing_run_id: str) -> None:
        super().__init__(
            f"strategy_instance_id {strategy_instance_id!r} already belongs to run "
            f"{existing_run_id!r}"
        )
        self.strategy_instance_id = strategy_instance_id
        self.existing_run_id = existing_run_id


class SpecOrAuditMissingError(DeployError):
    """The strategy spec or QC audit-copy path does not exist on disk."""


class DeployIOError(DeployError):
    """A filesystem error reading the inputs or writing the ledger that is not a
    plain missing-file (e.g. a permission error, or the spec path is a
    directory). Maps to infra-failure status rather than a 500 traceback."""


class ExplicitSurfaceSizingMismatchError(DeployError):
    """ADR 0009 § 6 — the strategy is registered with ``sizing_surface="explicit"``
    but the operator (or a stale client) submitted a policy-style sizing
    (e.g. ``FixedShares``). Maps to HTTP 400 — the operator must submit
    ``StrategyExplicit`` for explicit-surface strategies."""


class SizingPolicyMissingError(DeployError):
    """VCR-0001 / Phase 1 — ``live_config`` is empty or has no ``sizing`` key.

    Closes the back door where a missing policy fell through to legacy
    ``SimpleFloorSizing`` (the ``set_holdings(SPY, 1.0)`` all-in path). Maps to
    HTTP 400: the operator must submit an explicit policy. The Safe canary
    (``{"kind": "FixedShares", "value": 1}``) is the recommended starting
    point."""


class UnknownLiveConfigKeyError(DeployError):
    """VCR-0001 / Phase 1 — ``live_config`` carries a sibling key that
    ``_live_config_from_ledger`` does not know how to round-trip.

    Reject at the deploy boundary so the bad key is never hashed into
    ``run_id``. Otherwise the ledger persists with a field the runtime will
    refuse to interpret, leaving an unstartable run on disk."""


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
    # The hand-coded algorithm module the run starts under (#416). Recorded in
    # the ledger so the console defaults the Start card from it and `run start`
    # rejects a mismatched --strategy. Not hashed into run_id; "" = unrecorded.
    strategy_key: str = ""
    clean_tree_scope: tuple[str, ...] = DEFAULT_CLEAN_TREE_SCOPE
    force: bool = False
    idempotent: bool = False


@dataclass(frozen=True)
class DeployResult:
    run_id: str
    run_dir: Path
    created: bool
    ledger: LiveRunLedger


def _enforce_sizing_policy_present(live_config: dict) -> dict:
    """VCR-0001 / Phase 1 — refuse a new deploy whose ``live_config`` does not
    name an explicit sizing policy or whose siblings the ledger reader cannot
    round-trip.

    Returns the ``live_config`` with ``sizing`` re-serialized through the
    discriminated union's canonical form (mirrors the schema validator); the
    canonical form keeps ``run_id`` stable regardless of how the operator
    stringified ``Decimal`` on the wire.

    Three gates:

    1. Empty / missing ``sizing`` → :class:`SizingPolicyMissingError`. This is
       the actual hole VCR-0001 documented: an empty payload fell through to
       legacy ``SimpleFloorSizing``.
    2. Unknown siblings → :class:`UnknownLiveConfigKeyError`. Mirrors
       ``_live_config_from_ledger``'s allow-list so a stale CLI / typo never
       writes a ledger the runtime cannot read.
    3. Malformed ``sizing`` → :class:`SizingPolicyMissingError` wrapping the
       parser's ``ValueError`` (same error surface — the operator's next step
       is to fix the policy either way).
    """
    if not isinstance(live_config, dict) or not live_config:
        raise SizingPolicyMissingError(
            "live_config.sizing is required — Phase 1 / ADR 0009 closes the "
            "empty-live_config back door (VCR-0001). Submit an explicit "
            "policy (Safe canary: {'sizing': {'kind': 'FixedShares', 'value': 1}})."
        )
    from app.engine.execution.order_sizer import (
        parse_sizing_policy,
        policy_to_ledger_dict,
    )
    from app.engine.live.config import LIVE_CONFIG_LEDGER_KEYS

    unknown = set(live_config.keys()) - LIVE_CONFIG_LEDGER_KEYS
    if unknown:
        raise UnknownLiveConfigKeyError(
            f"unknown live_config keys: {sorted(unknown)}. Allowed keys: "
            f"{sorted(LIVE_CONFIG_LEDGER_KEYS)}."
        )
    sizing = live_config.get("sizing")
    if sizing is None:
        raise SizingPolicyMissingError(
            "live_config.sizing is required — Phase 1 / ADR 0009 closes the "
            "empty-live_config back door (VCR-0001). Submit an explicit "
            "policy (Safe canary: {'sizing': {'kind': 'FixedShares', 'value': 1}})."
        )
    try:
        policy = parse_sizing_policy(sizing)
    except ValueError as exc:
        raise SizingPolicyMissingError(str(exc)) from exc
    canonical = dict(live_config)
    canonical["sizing"] = policy_to_ledger_dict(policy)
    return canonical


def _enforce_explicit_surface_policy(strategy_key: str, live_config: dict) -> None:
    """ADR 0009 § 6 — refuse a policy-style ``live_config.sizing`` for a
    strategy registered with ``sizing_surface="explicit"``.

    Looks up the strategy in ``_STRATEGY_REGISTRY`` (re-using the same module-
    name → registry-key fallback as ``_lookup_sizing_surface`` in ``run.py``).
    A strategy that is unregistered, registered as ``policy``, or registered
    without a ``sizing_surface`` attribute is silently allowed — the runtime
    fail-fast remains the backstop for those cases.
    """
    if not isinstance(live_config, dict):
        return
    sizing = live_config.get("sizing")
    if not isinstance(sizing, dict):
        return
    try:
        from app.routers.engine import _STRATEGY_REGISTRY
    except Exception:
        return
    # VCR-0004 / Phase 2 — the registry is keyed by module name now, so the
    # legacy ``removeprefix("spy_")`` workaround is gone.
    reg = _STRATEGY_REGISTRY.get(strategy_key)
    surface = getattr(reg, "sizing_surface", None) if reg is not None else None
    if surface != "explicit":
        return
    if sizing.get("kind") != "StrategyExplicit":
        raise ExplicitSurfaceSizingMismatchError(
            f"strategy {strategy_key!r} is registered with sizing_surface='explicit' — "
            f"live_config.sizing must be {{'kind': 'StrategyExplicit'}}, got "
            f"{sizing.get('kind')!r}. The strategy sizes itself via internal accounting; "
            "the deploy-page policy cannot meaningfully reinterpret its quantity."
        )


def _existing_run_for_strategy_instance(
    run_root: Path, strategy_instance_id: str, *, allow_run_id: str
) -> str | None:
    if not run_root.is_dir():
        return None
    for run_dir in run_root.iterdir():
        if not run_dir.is_dir() or run_dir.name == allow_run_id:
            continue
        ledger_path = run_dir / "run_ledger.json"
        if not ledger_path.is_file():
            continue
        try:
            ledger = read_ledger(ledger_path)
        except (OSError, ValueError):
            continue
        if ledger.strategy_instance_id == strategy_instance_id:
            return ledger.run_id
    return None


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
    # Validate the operator-supplied instance id first: it is cheap, deterministic,
    # and a bad name should fail fast before any git work. Empty means "unbound"
    # (a legacy/deploy-only run) and is left to the operate layer.
    if params.strategy_instance_id:
        try:
            validate_strategy_instance_id(params.strategy_instance_id)
        except ValueError as exc:
            raise InvalidInstanceIdError(str(exc)) from exc

    repo_root = params.repo_root.resolve()
    scope_paths = [Path(p) for p in params.clean_tree_scope]

    clean = check_clean_tree(scope_paths, repo_root=repo_root)
    if not clean.passed:
        raise DirtyTreeError(clean.detail)

    code_sha = git_head_sha(repo_root)

    # VCR-0001 / Phase 1 — every new deploy must carry an explicit sizing
    # policy. The schema validator already rejects the API path; this is the
    # CLI / direct-call seam's enforcement. Canonicalize the ``sizing`` dict
    # so the CLI path produces the same hashed ``run_id`` the API path would.
    canonical_live_config = _enforce_sizing_policy_present(params.live_config)

    # ADR 0009 § 6 / PR7 reviewer fix — explicit-surface strategies size
    # themselves via internal accounting; the deploy boundary must refuse a
    # policy-style ``live_config.sizing`` for them so the ledger never
    # carries a misleading "live_config-governed" stamp that the runtime
    # fail-fast also catches. (Without this, a stale frontend or direct
    # daemon caller could submit ``FixedShares(1)`` for ``ema_crossover_options``
    # and we'd hash a misleading run_id before the engine refuses on the
    # first set_holdings call.)
    _enforce_explicit_surface_policy(params.strategy_key, canonical_live_config)

    # ADR 0009 — record the audit copy path relative to the repo so a
    # later read (the start gate, the cockpit) can re-verify against the
    # canonical allow-list. The on-disk allow-list lives under the host
    # repo root, so the build_ledger lookup needs the same repo_root the
    # clean-tree check used.
    try:
        ledger = build_ledger(
            code_sha=code_sha,
            strategy_spec_path=params.strategy_spec_path,
            qc_audit_copy_path=params.qc_audit_copy_path,
            qc_cloud_backtest_id=params.qc_cloud_backtest_id,
            account_id=params.account_id,
            start_date_ms=params.start_date_ms,
            live_config=canonical_live_config,
            strategy_instance_id=params.strategy_instance_id,
            strategy_key=params.strategy_key,
            audit_copy_allow_list_root=repo_root,
        )
    except FileNotFoundError as exc:
        raise SpecOrAuditMissingError(str(exc)) from exc
    except OSError as exc:
        # IsADirectoryError / PermissionError / etc. reading the input paths —
        # not a plain missing file, but still a filesystem failure that must
        # stay inside the typed contract, not escape as a 500 traceback.
        raise DeployIOError(str(exc)) from exc

    if ledger.strategy_instance_id:
        existing_run_id = _existing_run_for_strategy_instance(
            params.run_root,
            ledger.strategy_instance_id,
            allow_run_id=ledger.run_id,
        )
        if existing_run_id is not None:
            raise StrategyInstanceIdAlreadyUsedError(
                ledger.strategy_instance_id,
                existing_run_id,
            )

    run_dir = params.run_root / ledger.run_id
    ledger_path = run_dir / "run_ledger.json"

    if run_dir.exists() and not params.force:
        if params.idempotent and ledger_path.is_file():
            try:
                existing = read_ledger(ledger_path)
            except (OSError, ValueError) as exc:
                raise RunAlreadyExistsError(ledger.run_id, run_dir) from exc
            # run_id is content-addressed and excludes strategy_instance_id, so a
            # re-deploy with the same inputs but a DIFFERENT instance binding is
            # NOT a safe no-op — returning the old ledger would attach a later
            # start() to the wrong durable instance. Require both to match.
            if (
                existing.run_id == ledger.run_id
                and existing.strategy_instance_id == ledger.strategy_instance_id
            ):
                return DeployResult(
                    run_id=ledger.run_id, run_dir=run_dir, created=False, ledger=existing
                )
            raise RunAlreadyExistsError(ledger.run_id, run_dir)
        raise RunAlreadyExistsError(ledger.run_id, run_dir)

    try:
        write_ledger(ledger_path, ledger)
    except OSError as exc:
        raise DeployIOError(str(exc)) from exc
    return DeployResult(run_id=ledger.run_id, run_dir=run_dir, created=True, ledger=ledger)
