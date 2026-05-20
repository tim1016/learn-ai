"""Canonical import path for the shared DataPolicy contract.

PR B (2026-05-19) introduces ``DataPolicy`` as a backend-neutral shared
shape. The dataclass definition lives in ``manifest.py`` because PR A
embeds it inside ``RunManifest``; this module re-exports it from a
neutral path so non-manifest callers (engine persistence, GraphQL
mapping, compare endpoint) don't reach into the LEAN-specific module.

``DataPolicyManifest`` is kept as a re-export alias for one deprecation
cycle. New code imports ``DataPolicy``.
"""

from __future__ import annotations

import warnings

from app.lean_sidecar.manifest import BarsSpec, DataPolicy

__all__ = ["BarsSpec", "DataPolicy", "DataPolicyManifest"]  # noqa: F822 — DataPolicyManifest is exposed via __getattr__


def __getattr__(name: str):
    if name == "DataPolicyManifest":
        warnings.warn(
            "DataPolicyManifest is renamed to DataPolicy; import from "
            "app.lean_sidecar.data_policy.DataPolicy. Alias removed in a "
            "later cleanup PR.",
            DeprecationWarning,
            stacklevel=2,
        )
        return DataPolicy
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
