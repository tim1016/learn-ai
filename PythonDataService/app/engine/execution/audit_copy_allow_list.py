"""ADR 0009 § 3 — Audit-copy sizing allow-list.

The "receipt" that backs a ``reference_native`` ``sizing_provenance`` claim:
a single indexed JSON file (``docs/references/audit-copy-sizing-allow-list.json``)
mapping ``audit_copy_path → known sizing rule``, with the audit copy's
``sha256`` re-verified against the on-disk file at load time.

Three lookup outcomes:

* **proven_match** — file's sha matches the entry's sha AND the
  registered rule equals the resolved live ``SizingPolicy``.
* **proven_mismatch** — sha matches but the registered rule differs
  from the resolved live policy.
* **cannot_prove** — entry absent, file missing, or sha mismatch
  (someone edited the audit copy after registration).

The ``Reference parity`` preset proceeds **only on proven_match**;
``cannot_prove`` and ``proven_mismatch`` both **block** the deploy
(the preset's name is a promise, breaking it silently is the audit-UX
failure this design exists to prevent — ADR 0009 § 3).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.engine.execution.order_sizer import (
    SetHoldings,
    SizingPolicy,
    parse_sizing_policy,
)


class AuditCopyAllowListError(RuntimeError):
    """The allow-list file is missing, malformed, or self-inconsistent."""


AuditCopyVerdict = Literal["proven_match", "proven_mismatch", "cannot_prove"]


@dataclass(frozen=True)
class AuditCopyAllowListEntry:
    """One row of the allow-list.

    ``rule`` is parsed through the ``SizingPolicy`` discriminated union at
    load time so a malformed entry surfaces immediately, never at deploy.
    """

    audit_copy_path: str
    audit_copy_sha256: str
    rule: SizingPolicy
    registered_at_ms: int
    registered_by: str
    note: str = ""


@dataclass(frozen=True)
class AuditCopyLookup:
    """Outcome of looking up an audit copy against the allow-list."""

    verdict: AuditCopyVerdict
    # The expected rule from the allow-list, when the entry exists. ``None``
    # when the entry is absent or the file/sha mismatched (the operator has
    # no canonical rule to display).
    expected_rule: SizingPolicy | None = None
    # The proposed live policy that was checked against ``expected_rule``.
    # ``None`` when the lookup is purely informational (no policy passed in).
    actual_rule: SizingPolicy | None = None
    # Operator-facing reason string. Always populated; safe to render in
    # the deploy form's inline gate status verbatim.
    detail: str = ""


DEFAULT_ALLOW_LIST_PATH = Path("docs/references/audit-copy-sizing-allow-list.json")


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_repo_path(path: str, *, repo_root: Path | None) -> Path:
    """Resolve a repo-relative audit-copy path to a concrete location.

    Tries ``repo_root / path`` first, falling back to the literal path so a
    caller that already provides an absolute path (tests) keeps working.
    """
    if repo_root is not None:
        candidate = repo_root / path
        if candidate.exists():
            return candidate
    return Path(path)


def load_allow_list(
    path: Path | None = None,
    *,
    repo_root: Path | None = None,
) -> list[AuditCopyAllowListEntry]:
    """Read + parse the allow-list JSON; **does not** re-verify file shas.

    Sha re-verification is per-lookup so a missing/edited audit copy yields
    a ``cannot_prove`` verdict on lookup, not a load-time error (the operator
    can still deploy other strategies whose audit copies are intact).

    When ``path`` is omitted the canonical ``DEFAULT_ALLOW_LIST_PATH`` is
    resolved against ``repo_root`` (when provided) so the daemon and tests
    don't need to know the literal location.
    """
    if path is None:
        path = (repo_root / DEFAULT_ALLOW_LIST_PATH) if repo_root else DEFAULT_ALLOW_LIST_PATH
    raw = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuditCopyAllowListError(f"allow-list at {path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise AuditCopyAllowListError(f"allow-list at {path} must be a JSON array")

    entries: list[AuditCopyAllowListEntry] = []
    seen_paths: set[str] = set()
    for i, item in enumerate(payload):
        if not isinstance(item, dict):
            raise AuditCopyAllowListError(f"allow-list entry {i} is not an object")
        try:
            audit_copy_path = str(item["audit_copy_path"])
            audit_copy_sha256 = str(item["audit_copy_sha256"])
            rule_payload = item["rule"]
            registered_at_ms = int(item["registered_at_ms"])
            registered_by = str(item["registered_by"])
        except KeyError as exc:
            raise AuditCopyAllowListError(
                f"allow-list entry {i} missing required field {exc}"
            ) from exc
        if audit_copy_path in seen_paths:
            raise AuditCopyAllowListError(
                f"duplicate audit_copy_path in allow-list: {audit_copy_path!r}"
            )
        seen_paths.add(audit_copy_path)
        try:
            rule = parse_sizing_policy(rule_payload)
        except ValueError as exc:
            raise AuditCopyAllowListError(
                f"allow-list entry {i} ({audit_copy_path}) has invalid rule: {exc}"
            ) from exc
        entries.append(
            AuditCopyAllowListEntry(
                audit_copy_path=audit_copy_path,
                audit_copy_sha256=audit_copy_sha256,
                rule=rule,
                registered_at_ms=registered_at_ms,
                registered_by=registered_by,
                note=str(item.get("note", "")),
            )
        )
    return entries


def _rules_equivalent(a: SizingPolicy, b: SizingPolicy) -> bool:
    """Are two sizing policies *rule-equivalent* (ADR 0009 § 3)?

    Same kind + same magnitude. For ``SetHoldings`` the fraction must match
    exactly as a ``Decimal`` (not a coincidental share-count match — that's
    the "1.0 vs 0.5 happens to round to the same int" trap).
    """
    if type(a) is not type(b):
        return False
    if isinstance(a, SetHoldings):
        assert isinstance(b, SetHoldings)
        return a.fraction == b.fraction
    # FixedShares / FixedNotional are not registered Reference-parity rules
    # in v1 (the ADR's Reference parity preset is SetHoldings(1.0) only), so
    # equivalence collapses to the kind check above.
    return True


def lookup(
    audit_copy_path: str,
    proposed_policy: SizingPolicy | None,
    *,
    entries: list[AuditCopyAllowListEntry] | None = None,
    repo_root: Path | None = None,
) -> AuditCopyLookup:
    """Look up ``audit_copy_path`` against the allow-list and verify the sha.

    ``proposed_policy`` is the live ``live_config.sizing`` the operator is
    about to deploy. When ``None``, the lookup is purely informational
    (returns ``proven_match`` if the entry exists and the sha verifies, with
    ``actual_rule = None``) — useful for surfacing the registered rule to
    the deploy form before the operator picks Reference parity.

    Always returns a verdict; never raises. A missing allow-list file is
    surfaced as ``cannot_prove``.
    """
    if entries is None:
        try:
            entries = load_allow_list(repo_root=repo_root)
        except (OSError, AuditCopyAllowListError) as exc:
            return AuditCopyLookup(
                verdict="cannot_prove",
                detail=f"audit-copy allow-list unavailable: {exc}",
            )

    entry = next((e for e in entries if e.audit_copy_path == audit_copy_path), None)
    if entry is None:
        return AuditCopyLookup(
            verdict="cannot_prove",
            actual_rule=proposed_policy,
            detail=(
                f"audit copy {audit_copy_path!r} is not registered in the "
                "allow-list; Reference parity is unavailable until its sha + rule are registered"
            ),
        )

    on_disk = _resolve_repo_path(entry.audit_copy_path, repo_root=repo_root)
    if not on_disk.exists():
        return AuditCopyLookup(
            verdict="cannot_prove",
            expected_rule=entry.rule,
            actual_rule=proposed_policy,
            detail=f"audit copy file is missing at {on_disk}",
        )

    actual_sha = _file_sha256(on_disk)
    if actual_sha != entry.audit_copy_sha256:
        return AuditCopyLookup(
            verdict="cannot_prove",
            expected_rule=entry.rule,
            actual_rule=proposed_policy,
            detail=(
                f"audit copy {audit_copy_path!r} sha changed since registration "
                f"(expected {entry.audit_copy_sha256[:12]}…, got {actual_sha[:12]}…); "
                "re-register before claiming Reference parity"
            ),
        )

    if proposed_policy is None:
        return AuditCopyLookup(
            verdict="proven_match",
            expected_rule=entry.rule,
            actual_rule=None,
            detail=f"audit copy proves rule {_describe_rule(entry.rule)}",
        )

    if _rules_equivalent(entry.rule, proposed_policy):
        return AuditCopyLookup(
            verdict="proven_match",
            expected_rule=entry.rule,
            actual_rule=proposed_policy,
            detail=f"audit copy proves {_describe_rule(entry.rule)} — Reference parity available",
        )

    return AuditCopyLookup(
        verdict="proven_mismatch",
        expected_rule=entry.rule,
        actual_rule=proposed_policy,
        detail=(
            f"audit copy rule is {_describe_rule(entry.rule)}; "
            f"your live sizing is {_describe_rule(proposed_policy)} — "
            "Reference parity refuses to claim a derivative as reference-native"
        ),
    )


def _describe_rule(policy: SizingPolicy) -> str:
    """Operator-facing one-liner for a SizingPolicy. Stable text — the deploy
    form's inline gate status renders this verbatim, so the wording is
    intentionally explicit."""
    if isinstance(policy, SetHoldings):
        return f"SetHoldings({policy.fraction})"
    return type(policy).__name__
