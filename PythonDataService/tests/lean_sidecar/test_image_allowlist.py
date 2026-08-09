"""Pinned LEAN image authorization regression tests."""

from __future__ import annotations

import pytest

from app.lean_sidecar.config import (
    ALLOWED_IMAGE_DIGESTS,
    HISTORICAL_LEAN_IMAGE_DIGEST_ARM64,
    LEAN_IMAGE_REPO,
    RECONCILIATION_FIXTURE_IMAGE_DIGESTS,
)
from app.lean_sidecar.runner import RunnerConfigurationError, _require_image_in_allowlist


def test_allowed_image_digests_exclude_historical_fixture_digest() -> None:
    assert HISTORICAL_LEAN_IMAGE_DIGEST_ARM64 is not None
    assert HISTORICAL_LEAN_IMAGE_DIGEST_ARM64 not in ALLOWED_IMAGE_DIGESTS

    with pytest.raises(RunnerConfigurationError, match="not in ALLOWED_IMAGE_DIGESTS"):
        _require_image_in_allowlist(HISTORICAL_LEAN_IMAGE_DIGEST_ARM64)


def test_reconciliation_fixture_image_digests_authorize_historical_arm64_digest() -> None:
    assert HISTORICAL_LEAN_IMAGE_DIGEST_ARM64 is not None
    assert HISTORICAL_LEAN_IMAGE_DIGEST_ARM64 in RECONCILIATION_FIXTURE_IMAGE_DIGESTS
    assert _require_image_in_allowlist(
        HISTORICAL_LEAN_IMAGE_DIGEST_ARM64,
        allowed_image_digests=RECONCILIATION_FIXTURE_IMAGE_DIGESTS,
    ) == f"{LEAN_IMAGE_REPO}@{HISTORICAL_LEAN_IMAGE_DIGEST_ARM64}"
