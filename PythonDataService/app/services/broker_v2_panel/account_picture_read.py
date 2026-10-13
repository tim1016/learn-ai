"""The account's picture, as the account's money read serves it (#3101).

What the pure picture (``app.broker.alpaca.clerk.account_picture``) is handed
is read here, under the custody fence the caller holds: the budget the money
bar was drawn from, each bot's option holdings (stopped bots included), and
each holding bot's exit estimate. Nothing is read from Alpaca that the money
read has not already read: the cash and the equity are the observation's, and
the quotes are the ones the bot page's exit estimate prices from.
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)

# Every authority kind, so a new one is a KeyError here and never a simulated account called a real one's.
_SIMULATED_WORLDS: dict[AuthorityKind, Literal["Dry Run", "Live Shadow"] | None] = {
    "real_paper": None, "real_live": None, "shadow": "Live Shadow", "synthetic": "Dry Run",
}


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
            logger.warning(
                "the books' option holdings cannot be counted; the picture's holdings figures are unknown",
                exc_info=True, extra={"action": "account_picture_holdings_unreadable", "account_id": repo.account_id},
            )
    estimates = {} if holdings is None else read_exit_estimates(repo, holdings, now_ms=now_ms)
    picture = account_picture(
        money.budget,
        AccountReadings(
            observed_at_ms=observed_at_ms, now_ms=now_ms, equity=equity,
            simulated=_SIMULATED_WORLDS[world],
        ),
        holdings,
        estimates,
        holdings_unread=why,
    )
    if picture.spoken_for.partition.state == "unknown":
        # A claim missing from the lines, or an order claim the books cannot split: a fault to repair, said here
        # as well as to the owner.
        logger.error(
            "what is spoken for cannot be added up to the cash",
            extra={
                "action": "account_picture_partition_unknown", "account_id": repo.account_id,
                "reason": picture.spoken_for.sentence,
            },
        )
    return AccountPictureView.from_picture(picture)


__all__ = ["read_account_picture"]
