"""Recovery CLI for explicit Shadow activation; normal activation lives in Configuration."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.broker.alpaca.active_binding import BrokerUnbound
from app.broker.alpaca.clerk.account_authority import AccountAuthorityIdentityError, require_real_account_id
from app.broker.alpaca.clerk.shadow_authority import activate_shadow_clerk_authority
from app.broker.alpaca.config import AlpacaSettings
from app.broker_configuration.cli_binding import effective_alpaca_settings


class ShadowOperatorRefusal(ValueError):
    """This command cannot be run as asked -- named evidence or a required value is absent."""


def _write(payload: Mapping[str, Any]) -> None:
    """One JSON object per invocation, on stdout.

    ``default=str`` is here for the reconciliation's ``Decimal`` prices; every
    temporal value in the payload is already ``int64 ms UTC``.
    """
    sys.stdout.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def _resolved_settings(supplied: AlpacaSettings | None) -> AlpacaSettings:
    """The effective revision's settings, or this CLI's own refusal.

    Called only from the two places that actually need a value nobody stated on
    the command line, so an invocation naming both flags never opens the
    profiles database. A binding that will not resolve -- nothing applied, an
    unreadable database, an unresolvable revision -- is "this command cannot be
    run as asked", which is exactly ``ShadowOperatorRefusal`` at exit ``1``;
    there is no fall back to stale environment settings (ADR 0060 Decision 7).
    """
    if supplied is not None:
        return supplied
    try:
        return effective_alpaca_settings()
    except BrokerUnbound as exc:
        raise ShadowOperatorRefusal(
            f"{exc.reason}: {exc.unbound.message} {exc.unbound.next_step}"
        ) from exc


def _activate(*, live_account_id: str, artifacts_root: Path) -> int:
    record = asyncio.run(
        activate_shadow_clerk_authority(
            live_account_id=live_account_id, artifacts_root=artifacts_root
        )
    )
    _write(asdict(record))
    return 0


def main(argv: list[str] | None = None, *, settings: AlpacaSettings | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.manage_alpaca_shadow", exit_on_error=False)
    parser.add_argument("--live-account-id", required=True)
    parser.add_argument("--artifacts-root", type=Path)
    parser.add_argument("operation", choices=("activate",))
    try:
        args = parser.parse_args(argv)
        account_id = require_real_account_id(args.live_account_id)
        root = args.artifacts_root or _resolved_settings(settings).clerk_dir
        return _activate(live_account_id=account_id, artifacts_root=root)
    except (AccountAuthorityIdentityError, ShadowOperatorRefusal, argparse.ArgumentError) as exc:
        _write({"error": str(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
