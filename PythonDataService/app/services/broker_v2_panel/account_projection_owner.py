"""Account-scoped projection owner (spec §15).

``AccountProjectionOwner`` owns the ``BotRollupCache`` for one broker account,
so callers never construct a fresh cache per request.  ``sync`` is the single
write path: it bootstraps a bot's rollup from the full journal the first time
the bot is seen, then folds only the *new* journal entries into the cache on
each subsequent poll.  The expensive FIFO recompute happens once per bot, not
once per bot per poll — reads stay fresh without an O(bots × journal) scan.

One owner instance is held per ``(broker, account_id)`` key via the
module-level ``_OWNERS`` dict.  Use ``get_or_create_owner`` to obtain
the singleton for an account.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.decision_journal import DecisionReceipt
from app.broker.alpaca.clerk.fills import project_instance_fills
from app.broker.alpaca.clerk.models import OrderJournalEntry
from app.broker.alpaca.clerk.rollup_cache import BotRollup, BotRollupCache
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import BotCatalogView
from app.services.broker_v2_panel.catalog_projection_service import build_catalog


class AccountProjectionOwner:
    """Owns the ``BotRollupCache`` for one broker account.

    ``sync`` keeps the cache fresh incrementally: a bot seen for the first time
    is bootstrapped from the full journal; on later polls only the journal tail
    past the consumed watermark is folded in.  ``on_fill_appended`` dedups on
    ``(account_id, event_key)``, so a re-folded entry is a safe no-op — the
    watermark is an optimization, not a correctness dependency.
    """

    def __init__(self, account_id: str, broker: str) -> None:
        self._account_id = account_id
        self._broker = broker
        self._cache = BotRollupCache()
        # Count of journal entries already folded into the cache.  Entries past
        # this index are the tail to fold on the next sync.
        self._watermark = 0

    def sync(
        self,
        entries: list[OrderJournalEntry],
        sids: list[str],
        *,
        decisions: dict[str, DecisionReceipt | None] | None = None,
    ) -> None:
        """Refresh the rollup cache from the journal (§15).

        - A ``sid`` not yet in the cache is bootstrapped from the full journal
          (its fills may predate the current watermark).
        - Bots already in the cache get only the journal tail
          (``entries[watermark:]``) folded in — O(known_bots × new_entries),
          never O(bots × journal).
        - A journal that shrank (compaction / rotation) triggers a full rebuild.

        Idempotent: re-folding an already-seen fill is a no-op (the cache dedups
        on ``(account_id, event_key)``).
        """
        if len(entries) < self._watermark:
            # Journal shrank — the watermark no longer indexes into it. Rebuild.
            self._cache = BotRollupCache()
            self._watermark = 0

        new_entries = entries[self._watermark :]
        known_before = set(self._cache.known_sids())

        # New bots: bootstrap from the full history (their fills may predate the
        # watermark, so the tail alone would miss them).
        for sid in sids:
            if sid not in known_before:
                self._cache.bootstrap_from_fills(
                    sid, list(project_instance_fills(sid, entries))
                )

        # Pre-existing bots: fold only the new tail. New bots already consumed
        # the tail during bootstrap above.
        if new_entries:
            for sid in known_before:
                for fill in project_instance_fills(sid, new_entries):
                    self._cache.on_fill_appended(fill)

        self._watermark = len(entries)

        if decisions:
            for sid, receipt in decisions.items():
                if receipt is not None:
                    self._cache.on_decision_appended(receipt, sid=sid)

    def snapshot_catalog(
        self,
        statuses: list[BotStatusView],
        *,
        mark_prices: dict[str, dict[str, float]] | None = None,
    ) -> list[BotCatalogView]:
        """Return the catalog roster using the owner's cache."""
        return build_catalog(
            statuses,
            self._cache,
            account_id=self._account_id,
            mark_prices=mark_prices,
        )

    def get_rollup(
        self,
        sid: str,
        *,
        mark_prices: dict[str, float] | None = None,
    ) -> BotRollup:
        """O(1) rollup read for one bot (exposure/pnl)."""
        return self._cache.get_rollup(sid, mark_prices=mark_prices)


_OWNERS: dict[str, AccountProjectionOwner] = {}


def get_or_create_owner(account_id: str, broker: str) -> AccountProjectionOwner:
    """Return (or create) the singleton owner for ``(broker, account_id)``."""
    key = f"{broker}:{account_id}"
    if key not in _OWNERS:
        _OWNERS[key] = AccountProjectionOwner(account_id=account_id, broker=broker)
    return _OWNERS[key]


def reset_owners_for_testing() -> None:
    """Clear all owners (test isolation)."""
    _OWNERS.clear()
