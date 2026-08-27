"""Shared Clerk-transaction-projection availability contract.

The IBKR-specific projection (journal tailing, Postgres persistence, the
Postgres-cursor history/detail readers) was retired with the rest of IBKR
account authority (PR-A of #1813) — see
``docs/superpowers/plans/2026-08-26-ibkr-decommission-closeout.md``.
``ClerkTransactionProjectionUnavailable`` survives here because the active
SQLite/Alpaca transaction projection (``app.services.sqlite_clerk_transaction_projection``)
and its HTTP-facing readers (``app.routers.brokers``,
``app.routers.account_pnl_attribution``) raise and catch it as their one
backend-authored "projection unavailable" signal.
"""

from __future__ import annotations


class ClerkTransactionProjectionUnavailable(RuntimeError):
    """The dedicated transaction projection cannot currently serve reads."""


__all__ = ["ClerkTransactionProjectionUnavailable"]
