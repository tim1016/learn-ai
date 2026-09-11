"""Proving a previously-bound Alpaca account owes nothing, before switching away.

``worker_binding``'s switch preflight asks one question of the account the
worker is about to abandon: is it *provably* clear? The answer that ships with
that module is "no", for every account, because a build with no probe cannot
prove anything. This is the probe that can — and it is written so the only way
it ever answers "clear" is by having actually read every place an obligation
can hide and found each one empty.

**Why the asymmetry.** The outcome the whole ceremony exists to prevent is a
position open at one account while the worker writes to another and nobody can
EXIT it. A probe that guesses wrong in the permissive direction produces
exactly that; a probe that guesses wrong in the refusing direction produces a
start that boots the last-effective revision and tells the operator why. So
every failure here — an activation record that will not parse, a database that
will not verify, a binding row that will not load, an exception nobody
anticipated — is ``readable=False``, which ``PriorObligations.is_clear`` treats
identically to an open position.

**It must not take the lease.** Opening a Clerk authority the ordinary way
acquires that account's execution lease
(``repository_lifecycle._acquire_execution_lease``), and a preflight that took
the lease on the account it is about to leave would *be* the bug it is
checking for. Every read below is lease-free: ``ActivationStore.latest``
touches no SQLite at all, ``verify_database`` documents that it neither leases
nor stamps ``last_open_at_ms``, and the custody reads run over a ``mode=ro``
connection with ``PRAGMA query_only``. The custody-transition mirror is
deliberately not consulted — reconciling it is a *write*, and the authority's
own hash chain is what ``verify_database`` already proves.

**Two roots, configured independently.** Bot bindings live under
``live_artifacts_root()/live_state/``; the account's custody database lives
under the Clerk volume's ``accounts/alpaca/``. Both are constructor arguments
so a test can point them at ``tmp_path``, and both fall back to the
deployment's own — resolved *inside* the guarded body, so an installation that
cannot even answer where its roots are refuses rather than raises through the
preflight.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from app.broker.alpaca.clerk.account_authority import custody_account_ids_for
from app.broker.alpaca.clerk.sqlite import reads
from app.broker.alpaca.clerk.sqlite.activation import ActivationStore
from app.broker.alpaca.clerk.sqlite.database_verification import verify_database
from app.broker.alpaca.clerk.sqlite.repository import DB_FILENAME
from app.broker.alpaca.clerk.sqlite.writes import account_paths, confined_account_file
from app.broker.ibkr.config import live_artifacts_root
from app.broker_configuration.runtime import resolve_clerk_dir
from app.broker_configuration.worker_binding import PriorObligations
from app.engine.live.bot_lifecycle_state import (
    BotLifecyclePhase,
    BotLifecycleStateRepo,
    stable_bot_lifecycle_state_path,
)
from app.services.bot_binding_repository import (
    BINDING_FILENAME,
    STRATEGY_INSTANCE_FILENAME,
    BrokerBotBinding,
    live_state_binding_repository,
)

logger = logging.getLogger(__name__)

ALPACA_BROKER = "alpaca"

#: The namespace segment ``BotBindingRepository`` joins onto its artifacts root.
#: Repeated here because the repo has no shared constant for it — every reader
#: of that tree spells the literal (``cutover_roster``, ``dev_reset``,
#: ``catalog_quarantine``, and the repository itself).
LIVE_STATE_DIRECTORY = "live_state"

#: How many identities a blocking fact names before it elides the rest. A
#: refusal reason is operator prose, not a roster dump.
MAX_NAMED = 3


class _Unprovable(Exception):
    """One named reason this probe cannot prove the account clear.

    Private on purpose: it never leaves the module. It exists so the fail-closed
    exits read as one-line ``raise`` statements next to the fact they refuse on,
    instead of threading a sentinel return through five call sites.
    """


class ClerkPriorAccountObligations:
    """The real probe behind ``worker_binding``'s ``PriorAccountObligations``.

    ``clerk_dir`` is the Clerk volume that holds ``accounts/alpaca/``;
    ``live_state_root`` is the artifacts root that *contains* ``live_state/``
    (the same argument ``live_state_binding_repository`` takes, and the same
    name ``instance_seal_hashes`` gives it). ``None`` means "whatever this
    deployment is configured with", resolved per observation rather than at
    construction so that a misconfigured installation refuses one preflight
    instead of failing to build a probe at all.
    """

    def __init__(
        self,
        *,
        clerk_dir: Path | None = None,
        live_state_root: Path | None = None,
    ) -> None:
        self._clerk_dir = clerk_dir
        self._live_state_root = live_state_root

    async def observe(self, account_id: str) -> PriorObligations:
        """Whether ``account_id`` is provably clear of obligations.

        The protocol is ``async`` because the preflight that calls it is, but
        every read here is blocking filesystem and SQLite work, so it goes to a
        worker thread — the same rule ``BrokerConfigurationService`` follows for
        its own store reads.
        """
        return await asyncio.to_thread(self._observe, account_id)

    def _observe(self, account_id: str) -> PriorObligations:
        """Run every check, converting any failure at all into a refusal.

        The bare ``except Exception`` is deliberate and is not a silent handler:
        it logs, and it returns the *refusing* answer. A fail-closed safety
        probe is the one place where an unanticipated error must not be allowed
        to read as "clear" — and enumerating the exception types of five
        subsystems would mean a type this module has not heard of escaping into
        the preflight, where ``resolve_worker_binding`` has no refusal path for
        it and the start would crash instead of booting last-effective.
        """
        try:
            return self._prove_clear(account_id)
        except _Unprovable as exc:
            return _unprovable(account_id, str(exc))
        except Exception as exc:
            return _unprovable(account_id, f"{type(exc).__name__}: {exc}")

    def _prove_clear(self, account_id: str) -> PriorObligations:
        clerk_dir = self._clerk_dir if self._clerk_dir is not None else resolve_clerk_dir()
        live_state_root = (
            self._live_state_root if self._live_state_root is not None else live_artifacts_root()
        )
        # Only the *live* account's own custody database is read. A shadow
        # rehearsal has a second one at ``accounts/alpaca/shadow:<account>/``
        # behind its own ``ShadowActivationStore``, and it is deliberately not
        # consulted: that world's trade port is ``NoSubmitAlpacaTradePort``, so
        # nothing it holds is exposure at the broker that a switch could strand.
        # A rehearsal bot still attached to the account is caught anyway, by
        # ``custody_account_ids_for`` on the binding side below.
        accounts_root, account_dir = account_paths(clerk_dir, account_id)
        # ``confined_account_file`` rather than ``account_dir / DB_FILENAME``:
        # a legitimate account directory can still hold a symlink named
        # ``clerk.db`` pointing outside the volume.
        db_path = confined_account_file(clerk_dir, account_id, DB_FILENAME)

        activation = ActivationStore(accounts_root).latest(account_id)
        if activation is None:
            return PriorObligations(
                account_id=account_id,
                blocking_facts=_never_activated_facts(
                    account_id,
                    account_dir=account_dir,
                    db_path=db_path,
                    live_state_root=live_state_root,
                ),
            )

        if not db_path.is_file():
            raise _Unprovable("the activated custody database is missing")
        # Pinning generation and identity to the activation record is what makes
        # a substituted database a refusal rather than a clean read of the wrong
        # account's obligations.
        verify_database(
            db_path,
            expected_account_id=account_id,
            expected_generation=activation.authority_generation,
            expected_db_identity=activation.db_identity_token,
        )
        return PriorObligations(
            account_id=account_id,
            blocking_facts=(
                *_custody_facts(db_path),
                *_binding_facts(account_id, live_state_root=live_state_root),
            ),
        )


def _never_activated_facts(
    account_id: str,
    *,
    account_dir: Path,
    db_path: Path,
    live_state_root: Path,
) -> tuple[str, ...]:
    """The obligations of an account this installation never activated custody for.

    Activation, not database existence, is what grants an authority the right to
    mutate a broker account — so an account with no activation record was never
    one this installation could have taken custody on, and it owes nothing *as
    custody*. That is the only reading under which "no record" may mean "clear",
    and it holds only while the disk agrees: a custody directory or a
    ``clerk.db`` sitting there without a record is evidence of something this
    probe cannot account for, and evidence it cannot account for is a refusal.

    Bindings and their lifecycle are still read. A nonterminal bot sealed to
    the account is an obligation whether or not custody was ever activated here.
    """
    if db_path.exists() or account_dir.exists():
        raise _Unprovable(
            "a custody directory exists for an account this installation never activated"
        )
    return _binding_facts(account_id, live_state_root=live_state_root)


def _custody_facts(db_path: Path) -> tuple[str, ...]:
    """Every obligation the account's own custody database can still name.

    Five reads, picked to cover exposure, unfinished intent and unreviewed
    broker activity without overlapping merely for thoroughness:

    * ``strategy_instances_with_live_custody`` is the bot-scoped union a
      catalog row's attention flag is derived from. Its unique contribution
      here is the arm nothing else covers — an ACTIVE run.
    * ``attributed_positions_by_symbol`` and ``reconcilable_effect_operations``
      are the account-wide reads. Both admit rows carrying no
      ``strategy_instance_id``, which the bot-scoped union excludes by design,
      so neither is redundant with it.
    * ``has_nonterminal_manual_order`` catches the manual operation
      ``reconcilable_effect_operations`` filters out: one whose linked order
      already reads terminal at the broker while the operation itself has not
      been settled.
    * ``external_orders`` covers activity the Clerk did not originate. Only the
      unacknowledged ones are outstanding; a reviewed one has been dispositioned
      by an operator.

    ``uncertain_orders`` is deliberately *not* called. Every order it returns
    belongs to an effect operation in state ``unknown``, which
    ``reconcilable_effect_operations`` admits unconditionally — it would restate
    a fact already counted, and a refusal reason that says the same thing twice
    reads as two problems.
    """
    with closing(_read_only_connection(db_path)) as conn:
        live_custody = reads.strategy_instances_with_live_custody(conn)
        # ``!= 0.0`` rather than ``folds.position_quantity_is_nonzero``: the
        # epsilon-aware test calls a dust quantity flat, which is the permissive
        # direction, and this probe never takes the permissive direction. It is
        # the same superset ``strategy_instances_with_live_custody`` chooses.
        positions = {
            symbol: quantity
            for symbol, quantity in reads.attributed_positions_by_symbol(conn).items()
            if quantity != 0.0
        }
        operations = reads.reconcilable_effect_operations(conn)
        manual_order_open = reads.has_nonterminal_manual_order(conn)
        unreviewed = [
            order for order in reads.external_orders(conn) if order.acknowledged_at_ms is None
        ]

    facts: list[str] = []
    if positions:
        facts.append(
            _quantified(len(positions), "open position", "open positions", names=sorted(positions))
        )
    if operations:
        facts.append(_quantified(len(operations), "unresolved order", "unresolved orders"))
    if manual_order_open:
        facts.append("an unfinished manual order")
    if live_custody:
        facts.append(
            _quantified(
                len(live_custody),
                "bot holding live custody",
                "bots holding live custody",
                names=live_custody,
            )
        )
    if unreviewed:
        facts.append(
            _quantified(
                len(unreviewed),
                "unreviewed order placed outside the bots",
                "unreviewed orders placed outside the bots",
            )
        )
    return tuple(facts)


def _read_only_connection(db_path: Path) -> sqlite3.Connection:
    """A write-incapable, lease-free handle on one account's custody database.

    Exactly the connection ``verify_database`` opens, for exactly its reason:
    ``mode=ro`` plus ``PRAGMA query_only`` cannot take the execution lease, and
    cannot stamp ``last_open_at_ms`` on an account the worker is leaving.
    """
    connection = sqlite3.connect(f"{db_path.resolve(strict=True).as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _binding_facts(account_id: str, *, live_state_root: Path) -> tuple[str, ...]:
    bound = _alpaca_bindings_on(account_id, live_state_root=live_state_root)
    if not bound:
        return ()
    return (
        _quantified(
            len(bound),
            "bot still bound to it",
            "bots still bound to it",
            names=[binding.strategy_instance_id for binding in bound],
        ),
    )


def _alpaca_bindings_on(account_id: str, *, live_state_root: Path) -> list[BrokerBotBinding]:
    """Nonterminal Alpaca bindings on this account; unreadable evidence refuses.

    ``BotBindingRepository.list_for_broker`` logs and skips a row it cannot
    parse. That is right for a listing surface — one bad row must not hide the
    rest — and wrong here, because the row we cannot read is precisely the row
    that might be the obligation. So the walk is repeated with the skip removed
    and a corrupt row propagates, making the probe unreadable.

    Two further departures from ``instance_seal_hashes``, which asks a similar
    question for arming:

    * A binding with no v2 ``sealed_program`` is **kept**. That skip is correct
      for arming (a legacy record cannot be armed) and wrong for obligations —
      a legacy bot on the prior account is still a bot on the prior account.
    * Only ``sealed_account_id`` attributes a binding to an account, so a legacy
      record that sealed no account is not counted against *this* one. It cannot
      be: counting every unattributable binding against every account would
      refuse every switch forever. What such a bot actually holds is answered by
      the account's own custody database instead.

    Archive and retire both commit RETIRED (ADR 0052), keeping immutable binding
    files for history. Read that terminal fact through the same lifecycle repo
    as BotTaskRegistry. Missing lifecycle evidence keeps the binding blocking;
    corrupt evidence propagates. Independent custody reads still block any
    exposure, active run or unfinished order attributed to a terminal bot.
    """
    root = Path(live_state_root) / LIVE_STATE_DIRECTORY
    if not root.is_dir():
        return []
    repository = live_state_binding_repository(live_state_root)
    admissible = custody_account_ids_for(account_id)
    bound: list[BrokerBotBinding] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not (
            (child / STRATEGY_INSTANCE_FILENAME).is_file() or (child / BINDING_FILENAME).is_file()
        ):
            continue
        binding = repository.read(child.name)
        if binding is None:
            # ``read`` does not always *raise* on a row it cannot materialise:
            # it returns ``None`` when ``current_run.json`` is missing, or when
            # the run record it names is gone. Those directories still pass the
            # filter above, and their ``strategy_instance.json`` may well carry
            # a ``sealed_account_id`` for the very account being proven clear —
            # so treating ``None`` as "not a binding" is a fail-open, and it is
            # the shape an ungraceful stop leaves behind, because
            # ``record_launch`` writes ``current_run.json`` last.
            raise _Unprovable(f"binding row {child.name!r} could not be read")
        if binding.broker != ALPACA_BROKER:
            continue
        if binding.sealed_account_id in admissible:
            lifecycle = BotLifecycleStateRepo(
                stable_bot_lifecycle_state_path(live_state_root, binding.strategy_instance_id)
            ).read()
            if lifecycle is None or lifecycle.phase is not BotLifecyclePhase.RETIRED:
                bound.append(binding)
    return bound


def _quantified(count: int, singular: str, plural: str, *, names: Sequence[str] = ()) -> str:
    """One blocking fact, phrased to sit inside ``"account X still has ..."``.

    Both forms are spelled out rather than derived. Every noun here is a phrase
    ("bot holding live custody"), and an ``f"{singular}s"`` rule pluralises the
    wrong word in each of them.
    """
    noun = singular if count == 1 else plural
    if not names:
        return f"{count} {noun}"
    listed = sorted(names)
    shown = ", ".join(listed[:MAX_NAMED])
    elided = ", and more" if len(listed) > MAX_NAMED else ""
    return f"{count} {noun} ({shown}{elided})"


def _unprovable(account_id: str, detail: str) -> PriorObligations:
    logger.warning(
        "Prior account obligations could not be established; the switch cannot proceed",
        extra={
            "action": "prior_obligations_unreadable",
            "account_id": account_id,
            "detail": detail,
        },
    )
    return PriorObligations(account_id=account_id, readable=False)


__all__ = ["ClerkPriorAccountObligations"]
