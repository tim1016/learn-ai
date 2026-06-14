"""ADR 0009 § 3 — Audit-copy sizing allow-list verdicts and sha re-verification."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.execution.audit_copy_allow_list import (
    AuditCopyAllowListError,
    load_allow_list,
    lookup,
)
from app.engine.execution.order_sizer import (
    FixedShares,
    SetHoldings,
)


def _write_allow_list(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _make_audit_copy(repo_root: Path, rel: str, content: str = "# canonical") -> str:
    full = repo_root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return hashlib.sha256(content.encode()).hexdigest()


def test_load_allow_list_parses_entries(tmp_path: Path) -> None:
    repo = tmp_path
    sha = _make_audit_copy(repo, "references/qc-shadow/Foo.py")
    allow_list_path = repo / "docs" / "references" / "audit-copy-sizing-allow-list.json"
    _write_allow_list(
        allow_list_path,
        [
            {
                "audit_copy_path": "references/qc-shadow/Foo.py",
                "audit_copy_sha256": sha,
                "rule": {"kind": "SetHoldings", "fraction": "1.0"},
                "registered_at_ms": 1700000000000,
                "registered_by": "inkant",
            }
        ],
    )

    entries = load_allow_list(allow_list_path)
    assert len(entries) == 1
    assert isinstance(entries[0].rule, SetHoldings)
    assert entries[0].rule.fraction == Decimal("1.0")


def test_load_allow_list_rejects_duplicate_paths(tmp_path: Path) -> None:
    sha = _make_audit_copy(tmp_path, "references/qc-shadow/Foo.py")
    allow_list_path = _write_allow_list(
        tmp_path / "allow.json",
        [
            {
                "audit_copy_path": "references/qc-shadow/Foo.py",
                "audit_copy_sha256": sha,
                "rule": {"kind": "SetHoldings", "fraction": "1.0"},
                "registered_at_ms": 0,
                "registered_by": "x",
            },
            {
                "audit_copy_path": "references/qc-shadow/Foo.py",
                "audit_copy_sha256": sha,
                "rule": {"kind": "SetHoldings", "fraction": "0.5"},
                "registered_at_ms": 0,
                "registered_by": "x",
            },
        ],
    )
    with pytest.raises(AuditCopyAllowListError, match="duplicate"):
        load_allow_list(allow_list_path)


def test_load_allow_list_rejects_invalid_rule(tmp_path: Path) -> None:
    sha = _make_audit_copy(tmp_path, "references/qc-shadow/Foo.py")
    allow_list_path = _write_allow_list(
        tmp_path / "allow.json",
        [
            {
                "audit_copy_path": "references/qc-shadow/Foo.py",
                "audit_copy_sha256": sha,
                "rule": {"kind": "Bogus"},
                "registered_at_ms": 0,
                "registered_by": "x",
            }
        ],
    )
    with pytest.raises(AuditCopyAllowListError, match="invalid rule"):
        load_allow_list(allow_list_path)


# ───────────────────────────── lookup verdicts ──────────────────────────────


def _setup_repo(tmp_path: Path, *, rule_fraction: str = "1.0") -> tuple[Path, str]:
    repo = tmp_path / "repo"
    sha = _make_audit_copy(repo, "references/qc-shadow/Foo.py")
    _write_allow_list(
        repo / "docs" / "references" / "audit-copy-sizing-allow-list.json",
        [
            {
                "audit_copy_path": "references/qc-shadow/Foo.py",
                "audit_copy_sha256": sha,
                "rule": {"kind": "SetHoldings", "fraction": rule_fraction},
                "registered_at_ms": 1700000000000,
                "registered_by": "inkant",
            }
        ],
    )
    return repo, sha


def test_lookup_proven_match(tmp_path: Path) -> None:
    repo, _ = _setup_repo(tmp_path)
    verdict = lookup(
        "references/qc-shadow/Foo.py",
        SetHoldings(fraction=Decimal("1.0")),
        repo_root=repo,
    )
    assert verdict.verdict == "proven_match"
    assert "Reference parity available" in verdict.detail


def test_lookup_proven_mismatch_when_rule_differs(tmp_path: Path) -> None:
    repo, _ = _setup_repo(tmp_path, rule_fraction="0.5")
    verdict = lookup(
        "references/qc-shadow/Foo.py",
        SetHoldings(fraction=Decimal("1.0")),
        repo_root=repo,
    )
    assert verdict.verdict == "proven_mismatch"
    assert "your live sizing" in verdict.detail


def test_lookup_cannot_prove_when_kind_differs(tmp_path: Path) -> None:
    """A FixedShares live policy against a SetHoldings audit rule is a
    proven_mismatch — different kinds are never rule-equivalent."""
    repo, _ = _setup_repo(tmp_path)
    verdict = lookup(
        "references/qc-shadow/Foo.py",
        FixedShares(value=1),
        repo_root=repo,
    )
    assert verdict.verdict == "proven_mismatch"


def test_lookup_cannot_prove_when_audit_copy_edited(tmp_path: Path) -> None:
    """Sha drift since registration ⇒ cannot_prove, never silent acceptance."""
    repo, _ = _setup_repo(tmp_path)
    # Tamper with the audit copy after registration.
    (repo / "references/qc-shadow/Foo.py").write_text("# edited", encoding="utf-8")
    verdict = lookup(
        "references/qc-shadow/Foo.py",
        SetHoldings(fraction=Decimal("1.0")),
        repo_root=repo,
    )
    assert verdict.verdict == "cannot_prove"
    assert "sha changed" in verdict.detail


def test_lookup_cannot_prove_when_audit_copy_missing(tmp_path: Path) -> None:
    repo, _ = _setup_repo(tmp_path)
    (repo / "references/qc-shadow/Foo.py").unlink()
    verdict = lookup(
        "references/qc-shadow/Foo.py",
        SetHoldings(fraction=Decimal("1.0")),
        repo_root=repo,
    )
    assert verdict.verdict == "cannot_prove"
    assert "missing" in verdict.detail


def test_lookup_cannot_prove_when_audit_copy_unregistered(tmp_path: Path) -> None:
    repo, _ = _setup_repo(tmp_path)
    _make_audit_copy(repo, "references/qc-shadow/Bar.py", content="# bar")
    verdict = lookup(
        "references/qc-shadow/Bar.py",
        SetHoldings(fraction=Decimal("1.0")),
        repo_root=repo,
    )
    assert verdict.verdict == "cannot_prove"
    assert "not registered" in verdict.detail


def test_lookup_informational_returns_registered_rule(tmp_path: Path) -> None:
    """Passing ``None`` for proposed_policy is the deploy-form's pre-select
    query — surfaces the registered rule without claiming a match."""
    repo, _ = _setup_repo(tmp_path)
    verdict = lookup("references/qc-shadow/Foo.py", None, repo_root=repo)
    assert verdict.verdict == "proven_match"
    assert isinstance(verdict.expected_rule, SetHoldings)
    assert verdict.expected_rule.fraction == Decimal("1.0")
    assert verdict.actual_rule is None


def test_lookup_cannot_prove_when_allow_list_missing(tmp_path: Path) -> None:
    """No allow-list file at the canonical path ⇒ cannot_prove (fail-closed)."""
    repo = tmp_path
    _make_audit_copy(repo, "references/qc-shadow/Foo.py")
    verdict = lookup(
        "references/qc-shadow/Foo.py",
        SetHoldings(fraction=Decimal("1.0")),
        repo_root=repo,
    )
    assert verdict.verdict == "cannot_prove"
    assert "unavailable" in verdict.detail
