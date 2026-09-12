"""Polygon → LEAN data-lake module.

Authority: docs/architecture/adrs/0049-data-lake-is-the-market-data-authority.md
This package is the ONLY writer to LEAN_DATA_WRITE_ROOT. No other module
in the Python data service may produce files under that root.
"""
