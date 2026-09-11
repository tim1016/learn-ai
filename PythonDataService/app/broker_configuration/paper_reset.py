"""Offline Paper configuration cleanup around the custody reset ceremony.

This is the sole authorized exception to revision retention. Both installation
locks and one SQLite transaction span the custody operation, so a refusal
preserves configuration and a worker cannot install a binding during cleanup.
Live revisions and append-only provenance remain in the existing database.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path

from app.broker_configuration.errors import BrokerConfigurationError, ProfilesDatabaseUnavailable
from app.broker_configuration.store import ProfilesStore, profiles_database_exists, profiles_database_path
from app.utils.advisory_lock import try_advisory_file_lock


class PaperConfigurationResetRefused(BrokerConfigurationError):
    """The installation cannot safely discard the requested Paper configuration."""

    reason = "paper_configuration_reset_refused"


@contextmanager
def paper_configuration_reset(*, account_id: str, clerk_dir: Path) -> Iterator[None]:
    """Commit account-scoped cleanup only after the custody reset succeeds.

    The caller owns Paper eligibility and the canonical custody reset. Lock
    paths match ``installation_worker`` and service ``selection_handover``;
    acquiring fresh descriptors deliberately refuses a reentrant reset too.
    An absent database is initialized, without creating an owner, so a success
    can retire environment bootstrap with a nonzero selection generation.
    """
    body_failed = False
    try:
        with ExitStack() as stack:
            # A broken volume/file link is unreadable state, not an absent
            # installation that this ceremony may initialize.
            profiles_database_exists(clerk_dir)
            db_path = profiles_database_path(clerk_dir)
            for target in (db_path.with_name("worker"), db_path.with_name("selection-handover")):
                if not stack.enter_context(try_advisory_file_lock(target)):
                    raise PaperConfigurationResetRefused(
                        "The broker worker or a configuration handover is still running.",
                        next_step="Stop the worker and wait for configuration operations to finish, then retry.",
                    )
            store = ProfilesStore.open(clerk_dir=clerk_dir)
            stack.callback(store.close)
            conn = stack.enter_context(store.transaction())
            # Validate the protected state before allowing irreversible custody
            # work. Missing schema evidence must refuse before the body runs.
            selection = store.read_selection()
            live_pin = conn.execute(
                "SELECT 1 FROM profile_revisions WHERE endpoint_mode = 'live' AND account_pin = ? LIMIT 1",
                (account_id,),
            ).fetchone()
            if live_pin is not None:
                raise PaperConfigurationResetRefused(
                    "Saved Live configuration identifies the requested account as Live.",
                    next_step="Choose the disposable Paper account; Live account history cannot be reset.",
                )
            trigger = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'trg_profile_revisions_no_delete'"
            ).fetchone()
            if trigger is None:
                raise ProfilesDatabaseUnavailable(
                    "The broker configuration database is missing its revision-retention guard.",
                    next_step="Restore the configuration schema before retrying the Paper reset.",
                )
            removed = {
                (row["profile_id"], row["revision"])
                for row in conn.execute(
                    "SELECT profile_id, revision FROM profile_revisions "
                    "WHERE endpoint_mode = 'paper' AND (account_pin = ? OR "
                    "(account_pin IS NULL AND profile_id IN ("
                    "SELECT profile_id FROM profile_revisions "
                    "WHERE endpoint_mode = 'paper' AND account_pin = ?)))",
                    (account_id, account_id),
                )
            }
            nickname = conn.execute(
                "SELECT 1 FROM account_nicknames WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            try:
                yield
            except BaseException:
                body_failed = True
                raise

            clear_staged = (selection.staged_profile_id, selection.staged_revision) in removed
            clear_effective = (selection.effective_profile_id, selection.effective_revision) in removed
            if removed or nickname or selection.apply_requested or selection.selection_generation == 0:
                updated = replace(
                    selection,
                    staged_profile_id=None if clear_staged else selection.staged_profile_id,
                    staged_revision=None if clear_staged else selection.staged_revision,
                    effective_profile_id=None if clear_effective else selection.effective_profile_id,
                    effective_revision=None if clear_effective else selection.effective_revision,
                    effective_account_id=None if clear_effective else selection.effective_account_id,
                    effective_acknowledged_at_ms=(None if clear_effective else selection.effective_acknowledged_at_ms),
                    apply_requested=False,
                    apply_requested_at_ms=None,
                    apply_requested_generation=None,
                    selection_generation=selection.selection_generation + 1,
                    last_apply_outcome=None if clear_effective else selection.last_apply_outcome,
                    last_apply_refusal_reason=None if clear_effective else selection.last_apply_refusal_reason,
                )
                if not store.write_selection(
                    conn,
                    updated,
                    previous_generation=selection.selection_generation,
                ):
                    raise PaperConfigurationResetRefused(
                        "The broker selection changed during the Paper reset.",
                        next_step="Retry the completed custody reset to finish configuration cleanup.",
                    )
            conn.execute("DELETE FROM account_nicknames WHERE account_id = ?", (account_id,))
            if removed:
                # DDL participates in this transaction. No other connection
                # can write while this guard is absent, and rollback restores
                # it even if deletion or reinstallation fails.
                conn.execute("DROP TRIGGER trg_profile_revisions_no_delete")
                conn.executemany(
                    "DELETE FROM profile_revisions WHERE profile_id = ? AND revision = ?",
                    removed,
                )
                conn.execute(trigger["sql"])
                conn.executemany(
                    "DELETE FROM broker_profiles WHERE profile_id = ? AND NOT EXISTS "
                    "(SELECT 1 FROM profile_revisions WHERE profile_id = broker_profiles.profile_id)",
                    ((profile_id,) for profile_id in {profile_id for profile_id, _ in removed}),
                )
    except (OSError, sqlite3.DatabaseError) as exc:
        if body_failed:
            raise
        raise ProfilesDatabaseUnavailable(
            "The broker configuration database or its reset locks could not be read or written.",
            next_step="Check the Clerk volume and retry the Paper reset to finish configuration cleanup.",
        ) from exc


__all__ = ["PaperConfigurationResetRefused", "paper_configuration_reset"]
