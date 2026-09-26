"""Priming this root needs before its modules import the app.

``tests/conftest.py`` does the same for the main root (#2450); this root
sits beside the package it tests, so a run that collects only it — a
developer's ``pytest app/engine/strategy/spec/tests`` or an xdist worker
whose shard holds just these files — must prime the same two facts itself:

* ``POLYGON_API_KEY``: ``app.config.Settings`` requires it at import.
* the Signal Program source anchor: ``test_spec_router.py`` imports
  ``app.main``, and the helpers imported before it already import declared
  program modules, so the anchor must run before collection does that.
"""

from __future__ import annotations

import os

os.environ.setdefault("POLYGON_API_KEY", "test-key-for-testing")

from app.services.program_source_anchor import record_imported_program_sources

record_imported_program_sources()
