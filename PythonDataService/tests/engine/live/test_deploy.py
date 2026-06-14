"""Tests for the shared deploy seam (ADR 0006).

``deploy_run`` shells out to ``git`` for the dirty-tree gate and the HEAD sha,
so the fixture builds a real on-disk git repo rather than mocking subprocess —
the same approach as ``test_run_cli``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.engine.live.deploy import (
    DeployIOError,
    DeployParams,
    DirtyTreeError,
    ExplicitSurfaceSizingMismatchError,
    InvalidInstanceIdError,
    RunAlreadyExistsError,
    SizingPolicyMissingError,
    SpecOrAuditMissingError,
    UnknownLiveConfigKeyError,
    deploy_run,
)

requires_git = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git binary not available in this environment",
)


def _init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)


@pytest.fixture
def repo_with_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Clean git repo with a committed spec + QC audit copy. Returns
    ``(repo_root, spec_path, qc_audit_path)``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    (repo / "PythonDataService").mkdir()
    spec_path = repo / "PythonDataService" / "spec.json"
    spec_path.write_text('{"strategy": "spy_ema_crossover"}', encoding="utf-8")

    (repo / "references" / "qc-shadow").mkdir(parents=True)
    qc_path = repo / "references" / "qc-shadow" / "SpyEmaCrossoverAlgorithm.py"
    qc_path.write_text("# QC audit copy\n", encoding="utf-8")

    subprocess.run(
        ["git", "add", "PythonDataService/spec.json", "references/qc-shadow/SpyEmaCrossoverAlgorithm.py"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "commit", "-q", "-m", "seed", "--no-gpg-sign"], cwd=repo, check=True)
    return repo, spec_path, qc_path


def _params(repo: Path, spec: Path, qc: Path, run_root: Path, **overrides: object) -> DeployParams:
    base = {
        "repo_root": repo,
        "strategy_spec_path": spec,
        "qc_audit_copy_path": qc,
        "qc_cloud_backtest_id": "bt-1",
        "account_id": "DU111",
        "start_date_ms": 1700000000000,
        "run_root": run_root,
        # Phase 1 / ADR 0009: every new deploy carries an explicit sizing policy.
        # The Safe canary (FixedShares(1)) is the deploy-form default; tests
        # that override ``live_config`` must still carry a ``sizing`` key or be
        # asserting the new rejection path.
        "live_config": {"symbol": "SPY", "sizing": {"kind": "FixedShares", "value": 1}},
    }
    base.update(overrides)
    return DeployParams(**base)  # type: ignore[arg-type]


@requires_git
def test_deploy_run_creates_ledger(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    repo, spec, qc = repo_with_inputs
    run_root = tmp_path / "live_runs"

    result = deploy_run(_params(repo, spec, qc, run_root))

    assert result.created is True
    assert result.run_dir == run_root / result.run_id
    ledger = json.loads((result.run_dir / "run_ledger.json").read_text(encoding="utf-8"))
    assert ledger["run_id"] == result.run_id
    assert ledger["account_id"] == "DU111"
    assert ledger["live_config"] == {
        "symbol": "SPY",
        "sizing": {"kind": "FixedShares", "value": 1},
    }


@requires_git
def test_deploy_run_rejects_instance_id_with_space(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """Regression: a deployment name the operate endpoints reject (e.g. one with
    a space, like "Deploy morning Jun 3") must fail at creation, not produce a
    run that can never be selected or started. The id is validated before the
    git work, so no run directory is written."""
    repo, spec, qc = repo_with_inputs
    run_root = tmp_path / "live_runs"

    with pytest.raises(InvalidInstanceIdError):
        deploy_run(
            _params(repo, spec, qc, run_root, strategy_instance_id="Deploy morning Jun 3")
        )
    assert not run_root.exists()


@requires_git
def test_deploy_run_accepts_valid_instance_id(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    repo, spec, qc = repo_with_inputs

    result = deploy_run(
        _params(
            repo,
            spec,
            qc,
            tmp_path / "live_runs",
            strategy_instance_id="deployment-validation-jun3",
        )
    )

    assert result.created is True
    ledger = json.loads((result.run_dir / "run_ledger.json").read_text(encoding="utf-8"))
    assert ledger["strategy_instance_id"] == "deployment-validation-jun3"


@requires_git
def test_deploy_run_refuses_dirty_tree(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    repo, spec, qc = repo_with_inputs
    spec.write_text('{"strategy": "changed"}', encoding="utf-8")  # dirty the tree in-scope

    with pytest.raises(DirtyTreeError):
        deploy_run(_params(repo, spec, qc, tmp_path / "live_runs"))
    assert not (tmp_path / "live_runs").exists()


@requires_git
def test_deploy_run_missing_spec_raises(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    repo, _spec, qc = repo_with_inputs
    with pytest.raises(SpecOrAuditMissingError):
        deploy_run(_params(repo, repo / "PythonDataService" / "nope.json", qc, tmp_path / "live_runs"))


@requires_git
def test_deploy_run_missing_audit_raises(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    repo, spec, _qc = repo_with_inputs
    missing_qc = repo / "references" / "qc-shadow" / "nope.py"
    with pytest.raises(SpecOrAuditMissingError):
        deploy_run(_params(repo, spec, missing_qc, tmp_path / "live_runs"))


@requires_git
def test_deploy_run_directory_as_spec_raises_io_error(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """A non-missing filesystem failure (spec path is a directory) stays inside
    the typed contract as DeployIOError, not an escaping IsADirectoryError."""
    repo, _spec, qc = repo_with_inputs
    a_dir = repo / "PythonDataService"  # exists, but is a directory
    with pytest.raises(DeployIOError):
        deploy_run(_params(repo, a_dir, qc, tmp_path / "live_runs"))


@requires_git
def test_deploy_run_idempotent_redeploy_is_noop(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    repo, spec, qc = repo_with_inputs
    run_root = tmp_path / "live_runs"

    first = deploy_run(_params(repo, spec, qc, run_root, idempotent=True))
    second = deploy_run(_params(repo, spec, qc, run_root, idempotent=True))

    assert first.created is True
    assert second.created is False
    assert second.run_id == first.run_id


@requires_git
def test_deploy_run_idempotent_different_instance_is_collision(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """run_id excludes strategy_instance_id, so a re-deploy with the same inputs
    but a DIFFERENT instance binding must NOT be a safe no-op — it would attach a
    later start() to the wrong durable instance. It's a collision."""
    repo, spec, qc = repo_with_inputs
    run_root = tmp_path / "live_runs"

    first = deploy_run(_params(repo, spec, qc, run_root, idempotent=True, strategy_instance_id="inst-A"))
    assert first.created is True

    with pytest.raises(RunAlreadyExistsError):
        deploy_run(_params(repo, spec, qc, run_root, idempotent=True, strategy_instance_id="inst-B"))

    # Same instance binding remains a safe no-op.
    same = deploy_run(_params(repo, spec, qc, run_root, idempotent=True, strategy_instance_id="inst-A"))
    assert same.created is False


@requires_git
def test_deploy_run_non_idempotent_collision_raises(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    repo, spec, qc = repo_with_inputs
    run_root = tmp_path / "live_runs"

    deploy_run(_params(repo, spec, qc, run_root, idempotent=False))
    with pytest.raises(RunAlreadyExistsError):
        deploy_run(_params(repo, spec, qc, run_root, idempotent=False))


@requires_git
def test_deploy_run_idempotent_corrupt_ledger_is_collision(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    repo, spec, qc = repo_with_inputs
    run_root = tmp_path / "live_runs"

    first = deploy_run(_params(repo, spec, qc, run_root, idempotent=True))
    (first.run_dir / "run_ledger.json").write_text("{ not json", encoding="utf-8")

    with pytest.raises(RunAlreadyExistsError):
        deploy_run(_params(repo, spec, qc, run_root, idempotent=True))


@requires_git
def test_deploy_run_force_overwrites(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    repo, spec, qc = repo_with_inputs
    run_root = tmp_path / "live_runs"

    first = deploy_run(_params(repo, spec, qc, run_root))
    again = deploy_run(_params(repo, spec, qc, run_root, force=True))

    assert again.created is True
    assert again.run_id == first.run_id


# ────────────────── ADR 0009 § 6 / PR7 reviewer fix ──────────────────


@requires_git
def test_deploy_run_rejects_policy_sizing_for_explicit_surface_strategy(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """A stale frontend or direct daemon caller submitting `FixedShares(1)`
    for `ema_crossover_options` (registered as `sizing_surface="explicit"`)
    is refused at the deploy boundary, so the misleading run_id is never
    hashed."""
    repo, spec, qc = repo_with_inputs
    run_root = tmp_path / "live_runs"

    params = _params(
        repo,
        spec,
        qc,
        run_root,
        strategy_key="spy_ema_crossover_options",
        live_config={"symbol": "SPY", "sizing": {"kind": "FixedShares", "value": 1}},
    )
    with pytest.raises(ExplicitSurfaceSizingMismatchError, match=r"sizing_surface=.explicit"):
        deploy_run(params)
    assert not run_root.exists()


@requires_git
def test_deploy_run_accepts_strategy_explicit_for_explicit_surface_strategy(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """The honest `{kind: StrategyExplicit}` value the deploy form submits
    for explicit-surface strategies must be accepted."""
    repo, spec, qc = repo_with_inputs
    run_root = tmp_path / "live_runs"

    params = _params(
        repo,
        spec,
        qc,
        run_root,
        strategy_key="spy_ema_crossover_options",
        live_config={"symbol": "SPY", "sizing": {"kind": "StrategyExplicit"}},
    )
    result = deploy_run(params)
    assert result.created is True


@requires_git
def test_deploy_run_allows_policy_sizing_for_policy_surface_strategy(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """Policy-surface strategies (the default) keep accepting any
    SizingPolicy — the explicit-surface gate is opt-in by registration."""
    repo, spec, qc = repo_with_inputs
    run_root = tmp_path / "live_runs"

    params = _params(
        repo,
        spec,
        qc,
        run_root,
        strategy_key="spy_ema_crossover",
        live_config={"symbol": "SPY", "sizing": {"kind": "FixedShares", "value": 1}},
    )
    result = deploy_run(params)
    assert result.created is True



# ────────────────── Phase 1 / VCR-0001 — sizing required at deploy ───


@requires_git
def test_deploy_run_rejects_empty_live_config(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """VCR-0001 / Phase 1 — ``deploy_run`` (CLI seam) refuses an empty
    ``live_config``. Mirrors the schema-layer 400 the API path returns; the
    CLI ``init-ledger`` path goes straight to ``deploy_run`` and cannot bypass
    the gate."""
    repo, spec, qc = repo_with_inputs
    run_root = tmp_path / "live_runs"

    with pytest.raises(SizingPolicyMissingError, match=r"live_config\.sizing is required"):
        deploy_run(_params(repo, spec, qc, run_root, live_config={}))
    assert not run_root.exists()


@requires_git
def test_deploy_run_rejects_live_config_without_sizing(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """VCR-0001 / Phase 1 — ``live_config`` carrying siblings (e.g. ``symbol``)
    but no ``sizing`` would also fall through to legacy ``SimpleFloorSizing``.
    Reject at the deploy seam before any ledger is written."""
    repo, spec, qc = repo_with_inputs
    run_root = tmp_path / "live_runs"

    with pytest.raises(SizingPolicyMissingError, match=r"live_config\.sizing is required"):
        deploy_run(_params(repo, spec, qc, run_root, live_config={"symbol": "SPY"}))
    assert not run_root.exists()


@requires_git
def test_deploy_run_rejects_unknown_live_config_sibling_key(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """VCR-0001 / Phase 1 — unknown siblings of ``sizing`` are refused at the
    deploy seam (not after the ledger is written), so a stale CLI / typo never
    produces an unstartable ledger."""
    repo, spec, qc = repo_with_inputs
    run_root = tmp_path / "live_runs"

    with pytest.raises(UnknownLiveConfigKeyError, match=r"unknown live_config keys"):
        deploy_run(
            _params(
                repo,
                spec,
                qc,
                run_root,
                live_config={
                    "future_field": 1,
                    "sizing": {"kind": "FixedShares", "value": 1},
                },
            )
        )
    assert not run_root.exists()


@requires_git
def test_deploy_run_canonicalizes_sizing_dict(
    repo_with_inputs: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """VCR-0001 / Phase 1 — the CLI seam canonicalizes ``sizing`` so an
    operator who writes ``"fraction": "1.0"`` and one who writes ``"fraction":
    "1.00"`` produce the same hashed ``run_id``. Mirrors the schema validator."""
    repo, spec, qc = repo_with_inputs
    run_root = tmp_path / "live_runs"

    result = deploy_run(
        _params(
            repo,
            spec,
            qc,
            run_root,
            live_config={"sizing": {"kind": "SetHoldings", "fraction": "1.0"}},
        )
    )
    ledger = json.loads((result.run_dir / "run_ledger.json").read_text(encoding="utf-8"))
    assert ledger["live_config"]["sizing"] == {"kind": "SetHoldings", "fraction": "1.0"}
