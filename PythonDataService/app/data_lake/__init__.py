"""Polygon → LEAN data-lake module.

Authority: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md
This package is the ONLY writer to LEAN_DATA_WRITE_ROOT. No other module
in the Python data service may produce files under that root.
"""
