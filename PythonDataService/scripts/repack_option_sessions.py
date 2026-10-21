"""Repack stored option sessions without vendor access; originals are retained.

Run from PythonDataService with its configured lake and catalog. The report is
written atomically and names sessions by canonical open ms. ``--underlying``
may repeat; omitting it covers SPY, QQQ and IWM. A failed session stops the
command with its typed reason. Rerunning is safe after a crash or new capture.

Example: python scripts/repack_option_sessions.py --report /tmp/repack.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path

from app.data_lake import catalog_client
from app.data_lake.option_lake import OPTION_MARKET, OPTION_PROVIDER, option_read_root
from app.data_lake.option_session import read_option_session
from app.data_lake.option_session_repack import repack_option_session
from app.data_lake.path_policy import OptionListingPath
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.utils.atomic_file import atomic_write_bytes


async def repack(underlyings: list[str], *, rebind_listing: bool = False) -> list[dict[str, int | str | bool]]:
    results: list[dict[str, int | str | bool]] = []
    await catalog_client.init_pool()
    try:
        for underlying in underlyings:
            records = await catalog_client.select_complete_metadata_artifacts(OPTION_MARKET, underlying, OPTION_PROVIDER, "raw")
            listing_folder = OptionListingPath(underlying, date(2000, 1, 3)).relative_path().parent
            days = sorted({date(int(Path(row.file_path).name[:4]), int(Path(row.file_path).name[4:6]),
                                int(Path(row.file_path).name[6:8]))
                           for row in records if Path(row.file_path).parent.as_posix() == listing_folder.as_posix()})
            for day in days:
                outcome = await repack_option_session(underlying, day, rebind_listing=rebind_listing)
                if outcome.failure is not None:
                    raise RuntimeError(f"{underlying} session {session_open_ms_utc(day)}: {outcome.failure.reason}: {outcome.failure.detail}")
                session = await asyncio.to_thread(read_option_session, underlying, day, check_underlying=True)
                if session is None or outcome.record is None:
                    raise RuntimeError("repack did not leave an admitted session")
                results.append({"underlying": underlying, "session_open_ms": session_open_ms_utc(day),
                    "complete": session.complete, "contracts_complete": session.contracts_complete,
                    "captured": len(session.captured),
                    "confirmed_empty": len(session.confirmed_empty), "unresolved": len(session.unresolved),
                    "source_bytes": sum(len(raw) for raw in session.original_bytes.values()),
                    "container_bytes": (option_read_root() / outcome.record.file_path).stat().st_size,
                    "reused": outcome.reused})
    finally:
        await catalog_client.close_pool()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--underlying", action="append", choices=("SPY", "QQQ", "IWM"))
    parser.add_argument("--rebind-listing", action="store_true", help="Explicitly rebind retained coverage after a listing refresh")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(repack(args.underlying or ["SPY", "QQQ", "IWM"], rebind_listing=args.rebind_listing))
    atomic_write_bytes(args.report, json.dumps({"sessions": report}, sort_keys=True, indent=2).encode())


if __name__ == "__main__":
    main()
