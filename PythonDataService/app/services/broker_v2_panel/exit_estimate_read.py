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

**Signal option structures use the current saved minute on every account.**
Their chart and current figure share the retained bid/ask evidence (#3368).
Until every held leg has a two-sided quote this minute, the figure is unknown.
Other option readers retain their existing simulated or live quote policy.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence

from app.broker.alpaca.clerk.account_authority import simulated_account_named
from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.exit_estimate import ExitEstimate, OwedClose, exit_estimate
from app.broker.alpaca.clerk.funding import Figure
from app.broker.alpaca.clerk.kept_option_quotes import (
    HELD_MARK_MAX_AGE_MS,
    KeptOptionQuote,
    KeptOptionQuotes,
    KeptOptionQuotesUnavailable,
)
from app.broker.alpaca.clerk.sqlite.option_entry import held_option_legs
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.scheduled_close import held_contracts
from app.broker.alpaca.clerk.sqlite.simulated_account import read_kept_option_quotes_ahead
from app.broker.alpaca.market_liveness import get_option_quotes
from app.options.contract import OptionContract
from app.options.quotes import HeldQuote
from app.options.requirement import ContractAtMultiplier
from app.options.resolver import Chain
from app.schemas.action_plan import parse_bot_action_plan, traded_instrument
from app.schemas.deployment_budget import ExitEstimateView, ExitLegQuoteView, FigureView
from app.services.option_preview_views import contract_view

logger = logging.getLogger(__name__)

_LIVE_QUOTES = "Live option quotes from this Clerk's IBKR feed"
_REAL_KEEPS_NONE = "this estimate reads this Clerk's live IBKR quotes"


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
    """Read an account's kept quotes before the custody fence is taken, never under it.

    What is kept is then already in memory when the estimate asks for it. A
    file that cannot be read is not an error here: the estimate itself says
    so, as its figures' reason.
    """
    read_kept_option_quotes_ahead(KeptOptionQuotes.beside(repo.db_path), account_id=repo.account_id)


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
    return estimate_of_legs(repo, strategy_instance_id, legs, now_ms=now_ms)


def read_exit_estimates(
    repo: ClerkSqliteRepository, holdings_by_bot: Mapping[str, Mapping[ContractAtMultiplier, int]], *, now_ms: int,
) -> dict[str, ExitEstimate]:
    """What getting out costs for each bot that holds an option contract, from the holdings the caller read (#3101).

    The legs are the account's own read of each bot's holdings, so no bot is
    read twice and the estimate is of exactly what the account's picture
    counts. A bot holding none has none. A bot whose legs cannot be worked
    out has an estimate whose figures are unknown, with the reason, and is
    logged: one bot never takes the account's picture down with it.

    Each holding bot asks for its own owed closes (``_owed_closes``, one
    ``held_contracts`` read of the account's positions per bot): an N+1 over
    the bots that hold options, cheap while their positions say nothing is
    owed and not worth a second read of the worklist for one account's few
    bots (#3101 review).
    """
    estimates: dict[str, ExitEstimate] = {}
    for sid, legs in sorted(holdings_by_bot.items()):
        if not any(legs.values()):
            continue
        try:
            estimates[sid] = estimate_of_legs(repo, sid, dict(legs), now_ms=now_ms)
        except ValueError as unworkable:
            logger.warning(
                "a bot's exit estimate could not be worked out; its figures are unknown",
                exc_info=True,
                extra={"action": "exit_estimate_unworkable", "account_id": repo.account_id, "strategy_instance_id": sid},
            )
            why = Figure.unknown(f"This bot's exit estimate could not be worked out: {str(unworkable).rstrip('.')}")
            estimates[sid] = ExitEstimate(net_close=why, gross_buy_back=why, quotes=(), conditions=())
    return estimates


def estimate_of_legs(
    repo: ClerkSqliteRepository, strategy_instance_id: str, legs: dict[ContractAtMultiplier, int], *, now_ms: int,
) -> ExitEstimate:
    """The estimate for ``legs`` a bot holds, priced from the quotes its account keeps or is shown."""
    symbols = sorted(symbol for (symbol, _), contracts in legs.items() if contracts)
    if _uses_minute_quotes(repo, strategy_instance_id):
        kept = KeptOptionQuotes.beside(repo.db_path)
        try:
            minute = kept.minutes(from_ms=now_ms // 60_000 * 60_000, to_ms=now_ms + 1)
        except KeptOptionQuotesUnavailable:
            minute = []
        return minute_exit_estimate(legs, minute, now_ms=now_ms,
                                    owed_closes=owed_closes_for_position(repo, strategy_instance_id, now_ms=now_ms))
    simulated = simulated_account_named(repo.account_id)
    if simulated is None:
        quotes, quotes_from, none_held = _live_quotes(symbols)
    else:
        quotes, quotes_from, none_held = _kept_quotes(repo, symbols, simulated=simulated, now_ms=now_ms)
    return exit_estimate(
        legs, quotes, now_ms=now_ms, quotes_from=quotes_from, none_held=none_held,
        owed_closes=owed_closes_for_position(repo, strategy_instance_id, now_ms=now_ms),
    )


def minute_exit_estimate(
    legs: Mapping[ContractAtMultiplier, int], rows: Sequence[KeptOptionQuote], *, now_ms: int,
    owed_closes: Sequence[OwedClose] | None = (),
) -> ExitEstimate:
    """Value the current held legs from one captured current-minute quote cut, without fallback."""
    minute = now_ms // 60_000 * 60_000
    quotes = {
        row.symbol: row.quote.held if row.quote.bid is not None and row.quote.ask is not None
        and now_ms - row.kept_at_ms <= HELD_MARK_MAX_AGE_MS else None
        for row in rows if row.minute_ms == minute and row.kept_at_ms <= now_ms
    }
    return exit_estimate(legs, quotes, now_ms=now_ms, quotes_from="Saved held-leg minute quotes from IBKR",
                         none_held="this minute has no usable two-sided quote for this leg", owed_closes=owed_closes)


def _uses_minute_quotes(repo: ClerkSqliteRepository, strategy_instance_id: str) -> bool:
    configuration = repo.bot_config(strategy_instance_id)
    if configuration is None:
        return False
    declared = json.loads(configuration.config_json)
    plan = declared.get("action_plan")
    return declared.get("options_bot") is None and plan is not None and traded_instrument(parse_bot_action_plan(plan)).kind == "option"


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


def owed_closes_for_position(repo: ClerkSqliteRepository, strategy_instance_id: str, *, now_ms: int) -> tuple[OwedClose, ...] | None:
    """The closes the Clerk owes this bot's position, and any a session ended on: its own read of what is held.

    ``held_contracts`` is the one read the Clerk's scheduled closes act on.
    The close owed in the session now (or the next one, before it opens)
    and the close missed in the session that ended are both carried, each
    from its own field, so that a missed close never hides the owed one. A
    contract whose entry could not be read makes the closes unknown:
    ``None``, which is said and never taken for none owed (``held_contracts``
    logs why).
    """
    read = held_contracts(repo, now_ms, strategy_instance_id=strategy_instance_id)
    if strategy_instance_id in read.unread:
        return None
    closes: list[OwedClose] = []
    for item in read.held:
        if item.owed is not None:
            closes.append(OwedClose(item.owed.kind, item.owed.due_ms, left_to_owner=item.left_to_owner))
        if item.missed is not None:
            closes.append(OwedClose(item.missed.kind, item.missed.due_ms, missed=True))
    return tuple(closes)


__all__ = ["estimate_of_legs", "exit_estimate_view", "minute_exit_estimate", "owed_closes_for_position",
           "read_exit_estimate", "read_exit_estimates", "read_quotes_ahead"]
