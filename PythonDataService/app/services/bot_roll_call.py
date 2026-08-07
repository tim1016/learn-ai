"""Compatibility access to durable Start offers.

Roll-call catalog projection was retired with the IBKR bot-control UI. The
retained runner Start boundary still validates and consumes previously issued
offers, so this module keeps only the repository access required by that gate.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.engine.live.bot_lifecycle_state import (
    BotLifecycleStateCorruptError,
    BotRollCallOfferRecord,
    BotRollCallOfferRepo,
    stable_bot_roll_call_offers_path,
)

logger = logging.getLogger(__name__)


def bot_roll_call_offer_repo(root: Path, sid: str) -> BotRollCallOfferRepo:
    return BotRollCallOfferRepo(stable_bot_roll_call_offers_path(root.parent, sid))


def active_roll_call_offer(
    root: Path,
    sid: str,
    *,
    now_ms: int,
) -> BotRollCallOfferRecord | None:
    return bot_roll_call_offer_repo(root, sid).active_offer(now_ms=now_ms)


def safe_active_roll_call_offer(
    root: Path,
    sid: str,
    *,
    now_ms: int,
) -> BotRollCallOfferRecord | None:
    """Read an offer without letting one corrupt sidecar break Start admission."""

    try:
        return active_roll_call_offer(root, sid, now_ms=now_ms)
    except BotLifecycleStateCorruptError as exc:
        logger.warning(
            "failed to read bot roll-call offers",
            extra={"strategy_instance_id": sid, "exception": repr(exc)},
        )
        return None


__all__ = [
    "active_roll_call_offer",
    "bot_roll_call_offer_repo",
    "safe_active_roll_call_offer",
]
