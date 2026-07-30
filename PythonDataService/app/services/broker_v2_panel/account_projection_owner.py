"""Account-scoped projection owner (spec §15).

``AccountProjectionOwner`` bootstraps rollups from the order journal exactly
once at cold-start, then serves immutable O(1) snapshots on every catalog or
panel request.  The owner is the single writer of the ``BotRollupCache`` for
one account, so callers never construct a fresh cache per request.

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

    The owner is bootstrapped once from the full order journal (cold-start) and
    then receives incremental updates via ``on_journal_append``.  Every
    subsequent catalog or rollup read is O(1) — no journal scan per request.
    """

    def __init__(self, account_id: str, broker: str) -> None:
        self._account_id = account_id
        self._broker = broker
        self._cache = BotRollupCache()
        self._bootstrapped = False

    @property
    def is_bootstrapped(self) -> bool:
        return self._bootstrapped

    def bootstrap(
        self,
        entries: list[OrderJournalEntry],
        sids: list[str],
        *,
        decisions: dict[str, DecisionReceipt | None] | None = None,
    ) -> None:
        """Build initial rollup state from the full journal (cold-start path, §15).

        Reads the journal once and projects attributed fills per bot.  After
        this call ``is_bootstrapped`` is True and all subsequent reads are O(1).
        May only be called once; subsequent calls are no-ops.
        """
        if self._bootstrapped:
            return
        for sid in sids:
            fills = list(project_instance_fills(sid, entries))
            self._cache.bootstrap_from_fills(sid, fills)
        if decisions:
            for sid, receipt in decisions.items():
                if receipt is not None:
                    self._cache.on_decision_appended(receipt, sid=sid)
        self._bootstrapped = True

    def on_journal_append(self, entry: OrderJournalEntry) -> None:
        """O(1) incremental update when a new fill entry arrives."""
        from app.broker.alpaca.clerk.fills import normalize_fill_event

        seen: set[tuple[str, str]] = set()
        for sid in self._cache.known_sids():
            from app.engine.live.order_identity import build_bot_order_namespace

            ns = build_bot_order_namespace(sid)
            fill = normalize_fill_event(entry, seen, ns)
            if fill is not None:
                from app.broker.alpaca.clerk.fills import FillRecord

                stamped = FillRecord(
                    account_id=fill.account_id,
                    sid=sid,
                    intent_id=fill.intent_id,
                    order_ref=fill.order_ref,
                    event_key=fill.event_key,
                    symbol=fill.symbol,
                    side=fill.side,
                    quantity=fill.quantity,
                    fill_price=fill.fill_price,
                    filled_at_ms=fill.filled_at_ms,
                    fee=fill.fee,
                )
                self._cache.on_fill_appended(stamped)
                break  # one fill entry belongs to at most one sid

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
