"""The account's picture, as the account's money read serves it (#3101).

What the pure picture (``app.broker.alpaca.clerk.account_picture``) is handed
is read here, under the custody fence the caller holds: the budget the money
bar was drawn from, each bot's option holdings (stopped bots included), and
each holding bot's exit estimate. Nothing is read from Alpaca that the money
read has not already read: the cash and the equity are the observation's, and
the quotes are the ones the bot page's exit estimate prices from.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from app.broker.alpaca.clerk.account_money import AccountMoney
from app.broker.alpaca.clerk.account_picture import AccountReadings, account_picture
from app.broker.alpaca.clerk.sqlite.account_option_requirement import held_signed_contracts_by_bot
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.options.requirement import ContractAtMultiplier
from app.schemas.account_authority import AuthorityKind
from app.schemas.deployment_budget import AccountPictureView
from app.services.broker_v2_panel.exit_estimate_read import read_exit_estimates

_SIMULATED_WORLDS: dict[str, Literal["Dry Run", "Live Shadow"]] = {"shadow": "Live Shadow", "synthetic": "Dry Run"}


def read_account_picture(
    repo: ClerkSqliteRepository, money: AccountMoney, *, world: AuthorityKind, observed_at_ms: int,
    equity: Decimal | None,
) -> AccountPictureView:
    """The picture of ``money``'s account, as one fenced read of its books and its quotes.

    ``equity`` is the reading's, ``None`` when it carried none, and
    ``observed_at_ms`` is when the reading behind ``money`` was taken. Books
    that cannot be counted in whole contracts leave every figure that stands
    on them unknown, and say why; the cash and what is spoken for are still
    told. The caller holds the custody fence.
    """
    holdings: dict[str, dict[ContractAtMultiplier, int]] | None
    why = ""
    now_ms = repo.clock()
    with repo.write_fence() as conn:
        try:
            holdings = held_signed_contracts_by_bot(conn)
        except ValueError as unreadable:
            holdings, why = None, str(unreadable)
    estimates = {} if holdings is None else read_exit_estimates(repo, holdings, now_ms=now_ms)
    picture = account_picture(
        money.budget,
        AccountReadings(
            observed_at_ms=observed_at_ms, now_ms=now_ms, equity=equity,
            simulated=_SIMULATED_WORLDS.get(world),
        ),
        holdings,
        estimates,
        holdings_unread=why,
    )
    return AccountPictureView.from_picture(picture)


__all__ = ["read_account_picture"]
