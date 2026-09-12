"""Lease-expiry sweep for the data lake catalog.

Slice 1b lands the primitive only. Slice 4 wires it onto a scheduler (cron
or asyncio background task).

Spec: docs/architecture/adrs/0049-data-lake-is-the-market-data-authority.md § 4.4
"""

from __future__ import annotations

import logging
import time

from app.data_lake.catalog_client import connection

logger = logging.getLogger(__name__)


async def reclaim_expired_leases() -> int:
    """Mark any 'fetching' row whose lease has expired as 'failed'.

    Returns the number of rows reclaimed. Callers can re-attempt those rows
    via catalog_client.steal_or_retry_minute_bar.
    """
    now_ms = int(time.time() * 1000)
    query = """
        UPDATE "DataLakeArtifacts"
           SET "Status" = 'failed',
               "LastError" = 'lease_expired',
               "LeaseOwner" = NULL,
               "LeaseExpiresAtMs" = NULL
         WHERE "Status" = 'fetching'
           AND "LeaseExpiresAtMs" < $1;
    """
    async with connection() as conn:
        result = await conn.execute(query, now_ms)
    n = int(result.rsplit(" ", 1)[-1])
    if n > 0:
        logger.info("data_lake.sweep: reclaimed %d expired leases", n)
    return n
