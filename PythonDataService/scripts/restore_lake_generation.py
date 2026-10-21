"""Restore a verified lake/catalog generation into empty targets; starts no service.

Run ``python -m scripts.restore_lake_generation`` from ``PythonDataService/``
(docs/runbooks/restore-data-lake.md). Without a checkout, run the copy of
``app/option_archive/lake_restore.py`` each generation carries instead.
"""
from __future__ import annotations

import asyncio
import logging

from app.option_archive.lake_restore import main

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
