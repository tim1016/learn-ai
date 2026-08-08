"""Pinned LEAN image allow-list regression tests."""

from __future__ import annotations

from app.lean_sidecar.config import ALLOWED_IMAGE_DIGESTS, HISTORICAL_LEAN_IMAGE_DIGEST_ARM64


def test_historical_arm64_fixture_digest_remains_allowlisted() -> None:
    assert HISTORICAL_LEAN_IMAGE_DIGEST_ARM64 is not None
    assert HISTORICAL_LEAN_IMAGE_DIGEST_ARM64 in ALLOWED_IMAGE_DIGESTS
