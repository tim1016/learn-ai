"""The bot page's read of what getting out of its held option position costs (#3100).

What the pure estimate (``app.broker.alpaca.clerk.exit_estimate``) is handed
is read here: the contracts the bot holds, the newest quote held for each, and
the closes the Clerk owes the position. Where the quotes come from depends on
the account.

**A Dry Run and a Live Shadow keep their quotes.** The Clerk's own keeping
hands over the mark of every contract a bot holds into the account's kept
quotes (``kept_option_quotes``), and a simulated close is priced from the
newest of them. The estimate reads the same file the same way
(``KeptOptionQuotes.newest``), so it prices a close as the simulation would.

**A real account keeps none.** What its quotes are is the live ones in this
process's own IBKR supervisor, and they exist only while the bot's run asks
for them. With none the estimate says so and states no price: no figure is
invented from a mark, a fill or a share of the underlying (ADR 0076).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from app.broker.alpaca.clerk.account_authority import simulated_account_named
from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.exit_estimate import ExitEstimate, OwedClose, exit_estimate
from app.broker.alpaca.clerk.kept_option_quotes import KeptOptionQuotes, KeptOptionQuotesUnavailable
from app.broker.alpaca.clerk.sqlite.option_entry import held_option_legs
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.scheduled_close import CloseStanding, scheduled_close_state
from app.broker.alpaca.market_liveness import get_option_quotes
from app.options.contract import OptionContract
from app.options.quotes import HeldQuote
from app.options.resolver import Chain
from app.schemas.deployment_budget import ExitEstimateView, ExitLegQuoteView, FigureView
from app.services.option_preview_views import contract_view

logger = logging.getLogger(__name__)

_LIVE_QUOTES = "Live option quotes from this Clerk's IBKR feed"
_REAL_KEEPS_NONE = "real accounts keep no option quotes"


def exit_estimate_view(estimate: ExitEstimate) -> ExitEstimateView:
    """The estimate as the bot page is served it: every dollar and every word as Python authored it."""
    return ExitEstimateView(
        net_close=FigureView.from_figure(estimate.net_close),
        gross_buy_back=FigureView.from_figure(estimate.gross_buy_back),
        quotes=tuple(
            ExitLegQuoteView(
                contract=contract_view(OptionContract.from_occ_symbol(quality.symbol)),
                contracts=quality.contracts,
                state=quality.state,
                age_ms=quality.age_ms,
                missing=quality.missing,
                # Exactly as quoted: a price is never rounded on its way to the page.
                gap_usd=None if quality.gap is None else format(quality.gap, "f"),
            )
            for quality in estimate.quotes
        ),
        conditions=estimate.conditions,
    )


def read_quotes_ahead(repo: ClerkSqliteRepository) -> None:
    """Read a simulated account's kept quotes before the custody fence is taken, never under it.

    What is kept is then already in memory when the estimate asks for it. A
    file that cannot be read is not an error here: the estimate itself says
    so, as its figures' reason.
    """
    if simulated_account_named(repo.account_id) is None:
        return
    try:
        KeptOptionQuotes.beside(repo.db_path).refresh()
    except KeptOptionQuotesUnavailable:
        logger.warning(
            "the option quotes kept for a simulated account cannot be read; its exit estimate is unknown",
            exc_info=True,
            extra={"action": "exit_estimate_kept_quotes_unreadable", "account_id": repo.account_id},
        )


def read_exit_estimate(repo: ClerkSqliteRepository, strategy_instance_id: str, *, now_ms: int) -> ExitEstimate | None:
    """What getting out of the option contracts ``strategy_instance_id`` holds costs now, or ``None`` when it holds none.

    Refuses as ``BudgetUnavailable`` the holdings the books cannot count in
    whole contracts, which the bot's money is unavailable for as well.
    """
    try:
        legs = held_option_legs(repo, strategy_instance_id)
    except ValueError as exc:
        raise BudgetUnavailable(
            f"The option contracts this bot holds cannot be counted, so what getting out costs is not worked out: {exc}"
        ) from exc
    if not legs:
        return None
    symbols = sorted(symbol for symbol, _ in legs)
    simulated = simulated_account_named(repo.account_id)
    if simulated is None:
        quotes, quotes_from, none_held = _live_quotes(symbols)
    else:
        quotes, quotes_from, none_held = _kept_quotes(repo, symbols, simulated=simulated, now_ms=now_ms)
    return exit_estimate(
        legs, quotes, now_ms=now_ms, quotes_from=quotes_from, none_held=none_held,
        owed_closes=_owed_closes(repo, strategy_instance_id, now_ms=now_ms),
    )


def _kept_quotes(
    repo: ClerkSqliteRepository, symbols: Sequence[str], *, simulated: str, now_ms: int
) -> tuple[dict[str, HeldQuote | None], str, str]:
    """The newest quote kept for each contract, as a simulated close prices from it."""
    quotes_from = f"Quotes kept for this {simulated}"
    kept = KeptOptionQuotes.beside(repo.db_path)
    try:
        newest = {symbol: kept.newest(symbol, at_or_before_ms=now_ms) for symbol in symbols}
    except KeptOptionQuotesUnavailable:
        return {}, quotes_from, f"the option quotes kept for this {simulated} cannot be read"
    return (
        {symbol: None if row is None else row.quote.held for symbol, row in newest.items()},
        quotes_from,
        f"no quote has been kept for it yet, and this {simulated} keeps one for each contract its bots hold",
    )


def _live_quotes(symbols: Sequence[str]) -> tuple[dict[str, HeldQuote | None], str, str]:
    """The live quote this process's supervisor holds for each contract, of the feed's current generation."""
    source = get_option_quotes()
    feed = None if source is None else source.option_feed()
    if source is None or feed is None:
        return {}, _LIVE_QUOTES, (
            f"{_REAL_KEEPS_NONE}, and this process has no live option quote source: the bot's run is not asking for any"
        )
    if feed.unhealthy is not None:
        return {}, _LIVE_QUOTES, f"{_REAL_KEEPS_NONE}, and the live quote feed is not healthy: {feed.unhealthy}"
    quotes: dict[str, HeldQuote | None] = {}
    chains: dict[str, Chain | None] = {}
    for symbol in symbols:
        root = OptionContract.from_occ_symbol(symbol).root
        if root not in chains:
            chains[root] = source.option_chain(root)
        chain = chains[root]
        watched = None if chain is None else chain.watched.get(symbol)
        quote = None if watched is None else watched.quote
        # A quote from before the feed's last reconnect says nothing of the market now.
        quotes[symbol] = quote if quote is not None and quote.generation == feed.generation else None
    return quotes, _LIVE_QUOTES, (
        f"{_REAL_KEEPS_NONE}, and a live one is held only while the bot's run asks for it: none is held for this contract"
    )


def _owed_closes(repo: ClerkSqliteRepository, strategy_instance_id: str, *, now_ms: int) -> tuple[OwedClose, ...] | None:
    """The closes the Clerk owes this bot's position: the screens' own read of them (``scheduled_close_state``).

    A close already missed, and one that left the bot flat, are not owed.
    ``None`` when the books' account of them cannot be read, which is said
    and logged, never taken for "none owed".
    """
    try:
        states = scheduled_close_state(repo, strategy_instance_id, now_ms=now_ms)
    except (LookupError, ValueError, AssertionError):
        logger.exception(
            "a bot's scheduled close could not be read; its exit estimate says so",
            extra={
                "action": "exit_estimate_scheduled_close_unreadable",
                "account_id": repo.account_id,
                "strategy_instance_id": strategy_instance_id,
            },
        )
        return None
    return tuple(
        OwedClose(state.kind, state.due_ms, left_to_owner=state.left_to_owner)
        for state in states
        if state.standing not in (CloseStanding.MISSED, CloseStanding.FLAT)
    )


__all__ = ["exit_estimate_view", "read_exit_estimate", "read_quotes_ahead"]
