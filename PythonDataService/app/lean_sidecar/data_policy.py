"""Canonical import path for the shared DataPolicy contract.

PR B (2026-05-19) introduces ``DataPolicy`` as a backend-neutral shared
shape. The dataclass definition lives in ``manifest.py`` because PR A
embeds it inside ``RunManifest``; this module re-exports it from a
neutral path so non-manifest callers (engine persistence, GraphQL
mapping, compare endpoint) don't reach into the LEAN-specific module.
"""

from __future__ import annotations

from app.lean_sidecar.manifest import BarsSpec, DataPolicy

__all__ = ["BarsSpec", "DataPolicy"]

