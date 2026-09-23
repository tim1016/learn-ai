"""Installation migration: move the whole installation between hosts (#2268).

A host-side tool, driven by ``scripts/migrate_installation.py``. It never
runs inside the data plane and imports nothing that needs the service's
settings, so it runs from a bare checkout with only the host venv.
"""
