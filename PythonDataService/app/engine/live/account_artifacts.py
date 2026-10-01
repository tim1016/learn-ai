"""Account-scoped artifact path guard for retained historical account evidence."""

from __future__ import annotations

import os
import re
from pathlib import Path

# Uppercase alphanumerics only, digit-led allowed: Alpaca live account ids are digit-led (ADR 0059 slice 7).
_ACCOUNT_ID_RE = re.compile(r"^[A-Z0-9]{2,}$")


class AccountArtifactError(ValueError):
    """Raised when an account artifact path or payload is invalid."""


def safe_account_artifact_id(account_id: str) -> str:
    """Return an account id that is safe to use as one artifact path segment."""

    if account_id != account_id.strip():
        raise AccountArtifactError(f"invalid account_id: {account_id!r}")
    match = _ACCOUNT_ID_RE.fullmatch(account_id)
    if match is None:
        raise AccountArtifactError(f"invalid account_id: {account_id!r}")
    matched_account_id = match.group(0)
    safe_account_id = os.path.basename(matched_account_id)
    if safe_account_id != matched_account_id:
        raise AccountArtifactError(f"invalid account_id: {account_id!r}")
    return safe_account_id


# Private compatibility alias for the older binding-ledger import. New callers
# should use the public name so the path-boundary contract is explicit.
_safe_account_path_segment = safe_account_artifact_id


def account_artifacts_root(artifacts_root: Path, account_id: str) -> Path:
    """Return the confined account artifact directory for one account id.

    ``account_id`` can arrive from URL path segments on operator recovery
    endpoints. Require the already-canonical account-id spelling, reconstruct
    the path component from the regex match, then resolve and assert it remains below
    ``<artifacts_root>/accounts``. The match-group reconstruction and resolved-prefix
    check mirror CodeQL's path-injection guidance.
    """
    # Keep the regex capture in this path builder rather than accepting the
    # result of a custom validator. CodeQL recognizes Match.group() as a
    # path-segment sanitizer, while it cannot always follow that fact across
    # a helper return value.
    match = _ACCOUNT_ID_RE.fullmatch(account_id)
    if match is None or account_id != account_id.strip():
        raise AccountArtifactError(f"invalid account_id: {account_id!r}")
    safe_account_id = match.group(0)
    resolved_root = os.path.realpath(os.path.join(os.fspath(artifacts_root), "accounts"))
    resolved = os.path.realpath(os.path.join(resolved_root, safe_account_id))
    root_prefix = resolved_root.rstrip(os.sep) + os.sep
    if not resolved.startswith(root_prefix):
        raise AccountArtifactError(f"path traversal detected for account_id: {account_id!r}")
    return Path(resolved)


__all__ = [
    "AccountArtifactError",
    "account_artifacts_root",
    "safe_account_artifact_id",
]
