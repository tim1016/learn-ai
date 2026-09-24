"""The account half of a lane's quiet answer (#2154, ADR 0063 Decision 2).

A draining lane tells the coordinator whether it is quiet. Three of the five
conditions are facts about the account this authority is composed against,
and only this layer can read them:

- **every working order on the account has ended** — the broker's own open
  order list is empty. Account-scoped, not lane-attributed: a hand-placed order
  can still fill after the lane says quiet, and nothing in the lane can cancel
  it, so it must block rather than be filtered out;
- **the account is flat** — the broker's own position list is empty, for the
  same reason. A hand-opened position blocks and the operator closes it at the
  broker; nothing here closes anything. Flat is also the lane's *own* custody
  (#2344): the ledger attributes no exposure, and no open episode's policy
  declares ``blocks_lane_quiet`` — ``EXIT_NOT_FLAT``, ``EXIT_STUCK``,
  ``POSITION_DRIFT`` and the others that say the Clerk does not know what it
  holds. A lane whose custody still believes it holds a position would act on
  the account again after handing it over, so a flat broker alone does not
  answer flat. An episode with no registered policy blocks;
- **no order intent is in flight** — ``reconcilable_effect_operations`` is
  empty account-wide. Not ``InstanceCustodyProof.unresolved_intent_refs``,
  which covers only effects in state ``unknown`` and would miss an
  ``accepted`` intent that can still create custody.

The remaining conditions belong elsewhere: "draining" is the registry's own
fact, and "no bot running" is the bot runner's.

**The re-read rule.** The broker read gathers orders and positions
concurrently with no consistency fence, so one read cannot establish that both
were empty at one instant. This answers quiet only when two complete reads,
the second issued after the first returned, both found the account empty and
the intent read between them found nothing in flight. Any work open across that
interval and still open at either read is seen. What can escape is work that
opened *and* ended entirely between two reads and left the account empty —
which leaves nothing to hand over. When the first read already shows something
open, the answer is "not quiet" and the second read is not taken: there is
nothing it could add.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.off_loop import to_thread
from app.broker.alpaca.clerk.sqlite.reconcile import read_account_open_work
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_policies import reason_policy
from app.broker.contract.errors import BrokerError
from app.broker.contract.ports import BrokerReadPort

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AccountQuietObservation:
    """The account's answer to three of lane quiet's five conditions.

    ``account_flat`` is the broker's position list *and* the lane's own
    custody (#2344): attributed-flat, with no open lane-quiet-blocking
    uncertainty episode. One field on the wire, so the coordinator's refusal
    names both halves in one phrase.

    ``observed_at_ms`` is when the first read was issued: the earliest instant
    the answer covers, so the coordinator's freshness window ages it from the
    start of the evidence rather than its end.
    """

    observed_at_ms: int
    broker_work_ended: bool
    account_flat: bool
    intents_resolved: bool


def _episode_blocks_lane_quiet(reason_code: str) -> bool:
    """Read the registry's declaration; an unregistered code fails closed."""
    policy = reason_policy(reason_code)
    return policy is None or policy.blocks_lane_quiet


def _custody_flat(repo: ClerkSqliteRepository) -> bool:
    """Whether the lane's own ledger holds nothing and doubts nothing (#2344).

    Logs which half blocks, by count and reason code only: the coordinator
    hears only the account-flat condition, never a symbol or a quantity.
    """
    exposed_symbols = sum(
        position_quantity_is_nonzero(quantity)
        for quantity in repo.attributed_positions_by_symbol().values()
    )
    open_episodes = sorted(
        {
            episode["reason_code"]
            for episode in repo.active_uncertainties()
            if _episode_blocks_lane_quiet(episode["reason_code"])
        }
    )
    if exposed_symbols or open_episodes:
        logger.info(
            "lane-quiet: the lane's custody is not flat",
            extra={
                "action": "lane_quiet_custody_not_flat",
                "account_id": repo.account_id,
                "attributed_symbol_count": exposed_symbols,
                "open_episode_reason_codes": open_episodes,
            },
        )
        return False
    return True


async def _read_once(
    repo: ClerkSqliteRepository, read: BrokerReadPort
) -> tuple[bool, bool, bool]:
    orders, positions = await read_account_open_work(read)
    in_flight = await to_thread(repo.reconcilable_effect_operations)
    custody_flat = await to_thread(lambda: _custody_flat(repo))
    return not orders, not positions and custody_flat, not in_flight


async def observe_account_quiet(
    repo: ClerkSqliteRepository, read: BrokerReadPort
) -> AccountQuietObservation | None:
    """Observe the account's quiet conditions, or ``None`` if the broker is unreadable.

    ``None`` is no answer, not a "not quiet" one: a lane that cannot read its
    broker has no evidence about orders or positions to report, and reporting
    them as outstanding would name conditions it never observed. The
    coordinator reads the missing answer as silence, which never passes.
    """
    observed_at_ms = repo.clock()
    try:
        first = await _read_once(repo, read)
        second = await _read_once(repo, read) if all(first) else first
    except BrokerError as exc:
        logger.warning(
            "lane-quiet observation could not read the broker",
            extra={
                "action": "lane_quiet_broker_unreadable",
                "account_id": repo.account_id,
                "error": str(exc),
            },
        )
        return None
    orders_ended, flat, resolved = (a and b for a, b in zip(first, second, strict=True))
    return AccountQuietObservation(
        observed_at_ms=observed_at_ms,
        broker_work_ended=orders_ended,
        account_flat=flat,
        intents_resolved=resolved,
    )


__all__ = ["AccountQuietObservation", "observe_account_quiet"]
