"""The configuration module's interface: profiles, revisions, selection, audit.

The small interface the plan asks for (§4): manage profiles and revisions,
verify an account, stage a selection and record an Apply, report staged versus
effective, and read the audit log. Storage, validation, conflict detection and
audit live behind it; runtime callers receive records, never rows.

Three rules this module exists to hold, none of which a route may reinterpret:

* **Staging governs nothing.** ``PUT /selection`` records what the operator
  picked. Only an Apply moves a revision toward effective, and only the worker
  writes the effective fields — after construction succeeds and it owns the
  execution lease (ADR 0060 Decision 4).
* **A stale edit conflicts, it never overwrites.** Every mutating call that can
  race carries the value it believes it is superseding.
* **Nothing here is a secret.** A revision references an opaque credential slot
  and stores nothing else about credentials; no value, fragment, length or
  environment-variable name enters a row, a return value or a log line.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import replace
from typing import Any

from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker_configuration import selection
from app.broker_configuration.envelope import ValidatedLiveEnvelope
from app.broker_configuration.errors import (
    AccountModeDisagreement,
    AccountPinMismatch,
    DisplayNameConflict,
    ProfileArchived,
    ProfileInUse,
    ProfileNotFound,
    RevisionConflict,
    RevisionIncomplete,
    RevisionNotFound,
    SelectionGenerationConflict,
)
from app.broker_configuration.records import (
    ALPACA_BROKER,
    REVISION_SCHEMA_VERSION,
    AccountNickname,
    BrokerProfile,
    ConfigurationEvent,
    CredentialSlotStatus,
    EndpointMode,
    InstallationSelection,
    LocalOwner,
    ObservedAccount,
    ProfileRevision,
    ProfileWithRevision,
)
from app.broker_configuration.seams import (
    AccountVerifier,
    CredentialSlotDirectory,
    UnconfiguredAccountVerifier,
    UnconfiguredCredentialSlotDirectory,
)
from app.broker_configuration.store import ProfilesStore, new_identifier
from app.utils.timestamps import Clock, now_ms_utc

logger = logging.getLogger(__name__)

MAX_EVENT_PAGE = 200

# The one place the display-name rule lives is ``ux_broker_profiles_live_name``
# in ``schema.py``. SQLite names the *columns*, not the index, when a partial
# unique index is violated ("UNIQUE constraint failed: broker_profiles.owner_id,
# broker_profiles.display_name"), so the translation below matches on the
# column. Pinned by test_two_live_profiles_cannot_share_a_display_name.
_DISPLAY_NAME_CONSTRAINT = "broker_profiles.display_name"


def revision_content_sha256(
    *,
    credential_slot: str,
    endpoint_mode: EndpointMode,
    live_envelope: ValidatedLiveEnvelope | None,
    schema_version: int = REVISION_SCHEMA_VERSION,
) -> str:
    """The canonical hash over a revision's configured, non-secret content.

    Deliberately **not** the envelope sha and never mistaken for it (contract
    §2.3). It covers what the operator chose — slot reference, endpoint mode,
    envelope values, payload version — and excludes the profile ID, the author,
    the timestamps and the account pin. Excluding the pin is what lets pinning
    bind an existing revision without changing the identity a stale-edit check
    compares against; excluding the profile ID is what lets a clone of
    unchanged content hash equal to its source.
    """
    payload: dict[str, Any] = {
        "broker": ALPACA_BROKER,
        "schema_version": schema_version,
        "credential_slot": credential_slot,
        "endpoint_mode": endpoint_mode,
        "live_envelope": None if live_envelope is None else live_envelope.to_mapping(),
    }
    return canonical_sha256(payload)


def _is_complete(
    *,
    credential_slot: str,
    endpoint_mode: EndpointMode,
    live_envelope: ValidatedLiveEnvelope | None,
) -> bool:
    """Whether a revision may be staged or applied.

    Content completeness only: a slot reference, and the envelope whenever the
    mode is live. The account pin is deliberately not required — an unpinned
    revision is bindable, and re-observing the account at apply and at startup
    is the worker's gate, not a save-time one.
    """
    if not credential_slot:
        return False
    return endpoint_mode != "live" or live_envelope is not None


class BrokerConfigurationService:
    """The one facade over the profiles database."""

    def __init__(
        self,
        *,
        store: ProfilesStore,
        operator_identity: str,
        clock: Clock = now_ms_utc,
        credential_slots: CredentialSlotDirectory | None = None,
        account_verifier: AccountVerifier | None = None,
    ) -> None:
        self._store = store
        self._operator_identity = operator_identity
        self._clock = clock
        self._slots = credential_slots or UnconfiguredCredentialSlotDirectory()
        self._verifier = account_verifier or UnconfiguredAccountVerifier()

    def close(self) -> None:
        self._store.close()

    # ---- owner ----------------------------------------------------------

    def owner(self) -> LocalOwner:
        """The local owner, generated once on first read (ADR 0060 D3).

        Seeded from ``PANEL_OPERATOR_IDENTITY``; the record is authoritative
        afterwards, and a later change to that setting does not rename it.
        """
        existing = self._store.read_owner()
        if existing is not None:
            return existing
        now = self._clock()
        candidate = LocalOwner(
            owner_id=new_identifier("owner"),
            display_label=self._operator_identity,
            created_at_ms=now,
            updated_at_ms=now,
        )
        try:
            with self._store.transaction() as conn:
                self._store.insert_owner(conn, candidate)
        except sqlite3.IntegrityError:
            # Another process bootstrapped first; its row is the owner.
            recorded = self._store.read_owner()
            if recorded is None:
                raise
            return recorded
        return candidate

    def rename_owner(self, *, display_label: str) -> LocalOwner:
        owner = self.owner()
        now = self._clock()
        with self._store.transaction() as conn:
            self._store.update_owner_label(conn, display_label=display_label, updated_at_ms=now)
            self._record_event(
                conn,
                actor=owner.owner_id,
                action="owner_renamed",
                previous_ref=owner.display_label,
                next_ref=display_label,
                recorded_at_ms=now,
            )
        return replace(owner, display_label=display_label, updated_at_ms=now)

    # ---- credential slots ----------------------------------------------

    def credential_slots(self) -> tuple[CredentialSlotStatus, ...]:
        return self._slots.list_slots()

    # ---- profiles -------------------------------------------------------

    def list_profiles(self, *, include_archived: bool = False) -> list[BrokerProfile]:
        return self._store.list_profiles(include_archived=include_archived)

    def read_profile(self, profile_id: str) -> ProfileWithRevision:
        profile = self._require_profile(profile_id)
        return ProfileWithRevision(
            profile=profile,
            latest_revision=self._store.latest_revision(profile_id),
        )

    def create_profile(
        self,
        *,
        display_name: str,
        credential_slot: str,
        endpoint_mode: EndpointMode,
        live_envelope: ValidatedLiveEnvelope | None,
    ) -> ProfileWithRevision:
        """Create a profile and its revision 1 in one transaction."""
        return self._create_profile(
            display_name=display_name,
            credential_slot=credential_slot,
            endpoint_mode=endpoint_mode,
            live_envelope=live_envelope,
            action="profile_created",
            previous_ref=None,
        )

    def update_profile(
        self,
        profile_id: str,
        *,
        display_name: str | None = None,
        archived: bool | None = None,
    ) -> BrokerProfile:
        """Metadata only. A rename alters no execution identity (ADR 0060 D4)."""
        profile = self._require_profile(profile_id)
        owner = self.owner()
        next_name = profile.display_name if display_name is None else display_name
        next_archived = profile.archived if archived is None else archived
        if next_name == profile.display_name and next_archived == profile.archived:
            return profile
        now = self._clock()
        with _DisplayNameGuard(next_name), self._store.transaction() as conn:
            # Inside the write transaction: a selection staged between the read
            # above and this write would otherwise let an in-use profile be
            # archived.
            if next_archived and not profile.archived:
                self._refuse_archiving_profile_in_use(profile_id)
            self._store.update_profile_metadata(
                conn,
                profile_id=profile_id,
                display_name=next_name,
                archived=next_archived,
                updated_at_ms=now,
            )
            self._record_event(
                conn,
                actor=owner.owner_id,
                action="profile_archived" if next_archived != profile.archived else "profile_renamed",
                profile_id=profile_id,
                previous_ref=profile.display_name,
                next_ref=next_name,
                recorded_at_ms=now,
            )
        return replace(profile, display_name=next_name, archived=next_archived, updated_at_ms=now)

    def clone_profile(self, profile_id: str, *, display_name: str) -> ProfileWithRevision:
        """Copy a profile's latest content into a new profile — never its pin."""
        source = self._require_profile(profile_id)
        latest = self._store.latest_revision(source.profile_id)
        if latest is None:
            raise RevisionNotFound(
                "That profile has no revision to clone.",
                next_step="Save a revision on the source profile first.",
            )
        return self._create_profile(
            display_name=display_name,
            credential_slot=latest.credential_slot,
            endpoint_mode=latest.endpoint_mode,
            live_envelope=latest.live_envelope,
            action="profile_cloned",
            previous_ref=selection.reference(source.profile_id, latest.revision),
        )

    # ---- revisions ------------------------------------------------------

    def list_revisions(self, profile_id: str) -> list[ProfileRevision]:
        self._require_profile(profile_id)
        return self._store.list_revisions(profile_id)

    def read_revision(self, profile_id: str, revision: int) -> ProfileRevision:
        self._require_profile(profile_id)
        return self._require_revision(profile_id, revision)

    def create_revision(
        self,
        profile_id: str,
        *,
        expected_revision: int,
        credential_slot: str,
        endpoint_mode: EndpointMode,
        live_envelope: ValidatedLiveEnvelope | None,
    ) -> ProfileRevision:
        """Append the next immutable revision, refusing a stale edit.

        Idempotent on retry: a repeat carrying the same content as the current
        latest returns that revision instead of minting a duplicate, whether the
        caller's ``expected_revision`` is the latest (a re-save of unchanged
        content) or the one before it (a retry whose first response was lost).

        The staleness check reads ``latest`` **inside** the write transaction.
        Read outside it, two writers could both see revision N and both insert
        N+1 — one would win on the primary key and the loser would surface a
        constraint error instead of the contract's ``revision_conflict``.
        """
        profile = self._require_profile(profile_id)
        if profile.archived:
            raise ProfileArchived(
                "That profile is archived and cannot take a new revision.",
                next_step="Restore the profile, then save your change.",
            )
        content_sha256 = revision_content_sha256(
            credential_slot=credential_slot,
            endpoint_mode=endpoint_mode,
            live_envelope=live_envelope,
        )
        owner = self.owner()
        now = self._clock()
        with self._store.transaction() as conn:
            latest = self._store.latest_revision(profile_id)
            if (
                latest is not None
                and latest.content_sha256 == content_sha256
                and expected_revision in (latest.revision, latest.revision - 1)
            ):
                return latest
            self._require_expected_revision(expected_revision, latest)
            revision = self._build_revision(
                profile_id=profile_id,
                revision=1 if latest is None else latest.revision + 1,
                credential_slot=credential_slot,
                endpoint_mode=endpoint_mode,
                live_envelope=live_envelope,
                author_owner_id=owner.owner_id,
                created_at_ms=now,
            )
            self._store.insert_revision(conn, revision)
            self._store.update_profile_metadata(
                conn,
                profile_id=profile_id,
                display_name=profile.display_name,
                archived=profile.archived,
                updated_at_ms=now,
            )
            self._record_event(
                conn,
                actor=owner.owner_id,
                action="revision_created",
                profile_id=profile_id,
                revision=revision.revision,
                previous_ref=None if latest is None else latest.content_sha256,
                next_ref=revision.content_sha256,
                recorded_at_ms=now,
            )
        return revision

    # ---- verification and pinning ---------------------------------------

    async def verify_account(self, profile_id: str, revision: int) -> tuple[ObservedAccount, ...]:
        """Read-only broker discovery for one revision. Never a mutation.

        The two methods below are the module's only ``async`` entry points, and
        the only reason is the verifier's network call. Their SQLite work goes
        through ``asyncio.to_thread`` for the same reason the router dispatches
        every synchronous call that way — a blocking read or an fsync must not
        stall the event loop.
        """
        stored = await asyncio.to_thread(self._read_bound_revision, profile_id, revision)
        return await self._verifier.observe_accounts(
            credential_slot=stored.credential_slot,
            endpoint_mode=stored.endpoint_mode,
        )

    async def pin_account(self, profile_id: str, revision: int, *, account_id: str) -> ProfileRevision:
        """Bind one explicitly selected *observed* account to a revision.

        Nobody types an account ID that reaches storage unchecked: the account
        must appear in a fresh read-only observation under this revision's mode,
        and a re-observation that contradicts an existing pin refuses without
        replacing it (contract §3).
        """
        stored = await asyncio.to_thread(self._read_bound_revision, profile_id, revision)
        observed = await self._verifier.observe_accounts(
            credential_slot=stored.credential_slot,
            endpoint_mode=stored.endpoint_mode,
        )
        match = next((account for account in observed if account.account_id == account_id), None)
        if match is None:
            raise AccountPinMismatch(
                f"Account {account_id} was not among the accounts observed for this revision.",
                next_step="Verify the account again and choose one of the observed accounts.",
            )
        if match.account_mode != stored.endpoint_mode:
            raise AccountModeDisagreement(
                f"The observed account is a {match.account_mode} account and this revision is "
                f"{stored.endpoint_mode}.",
                next_step="Change the revision's mode, or choose an account that matches it.",
            )
        if stored.account_pin is not None:
            if stored.account_pin != account_id:
                raise AccountPinMismatch(
                    f"This revision is already bound to account {stored.account_pin}.",
                    next_step="Create a new revision to bind a different account.",
                )
            return stored

        return await asyncio.to_thread(self._record_account_pin, profile_id, revision, account_id)

    def _record_account_pin(
        self, profile_id: str, revision: int, account_id: str
    ) -> ProfileRevision:
        owner = self.owner()
        now = self._clock()
        with self._store.transaction() as conn:
            bound = self._store.write_account_pin(
                conn,
                profile_id=profile_id,
                revision=revision,
                account_id=account_id,
                pinned_at_ms=now,
            )
            if not bound:
                # A concurrent pin won the write-once transition. Whatever it
                # bound stands; this request never replaces a recorded pin.
                raise AccountPinMismatch(
                    "This revision was bound to an account while you were verifying.",
                    next_step="Reload the revision to see the account it is bound to.",
                )
            self._record_event(
                conn,
                actor=owner.owner_id,
                action="account_pinned",
                profile_id=profile_id,
                revision=revision,
                next_ref=account_id,
                recorded_at_ms=now,
            )
        return self._require_revision(profile_id, revision)

    # ---- nicknames ------------------------------------------------------

    def list_nicknames(self) -> list[AccountNickname]:
        return self._store.list_nicknames()

    def set_nickname(self, account_id: str, *, nickname: str) -> AccountNickname:
        owner = self.owner()
        now = self._clock()
        record = AccountNickname(account_id=account_id, nickname=nickname, updated_at_ms=now)
        with self._store.transaction() as conn:
            self._store.upsert_nickname(conn, record)
            self._record_event(
                conn,
                actor=owner.owner_id,
                action="nickname_set",
                next_ref=account_id,
                recorded_at_ms=now,
            )
        return record

    # ---- selection ------------------------------------------------------

    def selection(self) -> InstallationSelection:
        """Staged **and** effective, so no surface can render one as the other."""
        return self._store.read_selection()

    def stage_selection(
        self, *, profile_id: str, revision: int, expected_selection_generation: int
    ) -> InstallationSelection:
        """Record what the operator picked. This governs nothing by itself."""
        current = self._store.read_selection()
        self._require_generation(current, expected_selection_generation)
        profile = self._require_profile(profile_id)
        if profile.archived:
            raise ProfileArchived(
                "That profile is archived and cannot be staged.",
                next_step="Restore the profile, then stage it.",
            )
        stored = self._require_revision(profile_id, revision)
        if not stored.complete:
            raise RevisionIncomplete(
                "That revision is a draft and cannot be staged.",
                next_step="Finish the revision — a live revision needs all six envelope values.",
            )
        if current.staged_profile_id == profile_id and current.staged_revision == revision:
            return current

        staged = selection.staged(current, profile_id=profile_id, revision=revision)
        return self._commit_selection(
            staged,
            previous_generation=current.selection_generation,
            action="selection_staged",
            profile_id=profile_id,
            revision=revision,
            previous_ref=selection.reference(current.staged_profile_id, current.staged_revision),
            next_ref=f"generation:{staged.selection_generation}",
        )

    def request_apply(self, *, expected_selection_generation: int) -> InstallationSelection:
        """Record the one-shot Apply. Changes no runtime (ADR 0060 D4).

        The staged revision becomes effective at the next controlled restart,
        which the operator performs; there is no ``ACTIVE_PROFILE`` variable and
        no container restart from this surface.
        """
        current = self._store.read_selection()
        if selection.is_recorded_apply(current, expected_selection_generation):
            return current
        self._require_generation(current, expected_selection_generation)
        if current.staged_profile_id is None or current.staged_revision is None:
            raise RevisionNotFound(
                "Nothing is staged, so there is nothing to apply.",
                next_step="Stage a profile revision first.",
            )
        profile = self._require_profile(current.staged_profile_id)
        if profile.archived:
            raise ProfileArchived(
                "The staged profile is archived and cannot be applied.",
                next_step="Restore the profile, or stage a different one.",
            )
        stored = self._require_revision(current.staged_profile_id, current.staged_revision)
        if not stored.complete:
            raise RevisionIncomplete(
                "The staged revision is a draft and cannot be applied.",
                next_step="Finish the revision, then stage and apply it.",
            )

        requested = selection.apply_requested(current, at_ms=self._clock())
        self._commit_selection(
            requested,
            previous_generation=current.selection_generation,
            action="apply_requested",
            profile_id=current.staged_profile_id,
            revision=current.staged_revision,
            previous_ref=f"generation:{current.selection_generation}",
            next_ref=f"generation:{requested.selection_generation}",
        )
        logger.info(
            "Broker configuration Apply requested",
            extra={
                "profile_id": current.staged_profile_id,
                "revision": current.staged_revision,
                "selection_generation": requested.selection_generation,
            },
        )
        return requested

    def acknowledge_effective(
        self,
        *,
        profile_id: str,
        revision: int,
        account_id: str | None,
        expected_selection_generation: int,
    ) -> InstallationSelection:
        """The worker's write, after construction succeeded under its lease.

        No route calls this. The generation fence is what stops a stale worker
        publishing itself as the effective runtime.
        """
        current = self._store.read_selection()
        self._require_generation(current, expected_selection_generation)
        self._require_revision(profile_id, revision)
        acknowledged = selection.effective_acknowledged(
            current,
            profile_id=profile_id,
            revision=revision,
            account_id=account_id,
            at_ms=self._clock(),
        )
        self._commit_selection(
            acknowledged,
            previous_generation=current.selection_generation,
            action="effective_acknowledged",
            profile_id=profile_id,
            revision=revision,
            previous_ref=selection.reference(
                current.effective_profile_id, current.effective_revision
            ),
            next_ref=selection.reference(profile_id, revision),
            result="applied",
        )
        logger.info(
            "Broker configuration effective binding acknowledged",
            extra={
                "profile_id": profile_id,
                "revision": revision,
                "selection_generation": acknowledged.selection_generation,
            },
        )
        return acknowledged

    def record_apply_refusal(
        self, *, reason: str, expected_selection_generation: int
    ) -> InstallationSelection:
        """A refused Apply consumes its one-shot request (ADR 0060 D4.3).

        Leaving it pending would let a later unattended restart apply exactly
        the change that was just refused. The effective binding is untouched:
        the worker boots the last-effective revision.
        """
        current = self._store.read_selection()
        self._require_generation(current, expected_selection_generation)
        refused = selection.apply_refused(current, reason=reason)
        self._commit_selection(
            refused,
            previous_generation=current.selection_generation,
            action="apply_refused",
            profile_id=current.staged_profile_id,
            revision=current.staged_revision,
            next_ref=reason,
            result="refused",
        )
        logger.warning(
            "Broker configuration Apply refused",
            extra={
                "reason": reason,
                "profile_id": current.staged_profile_id,
                "revision": current.staged_revision,
            },
        )
        return refused

    # ---- events ---------------------------------------------------------

    def events(self, *, limit: int = 50, before_event_id: str | None = None) -> list[ConfigurationEvent]:
        before_sequence: int | None = None
        if before_event_id is not None:
            before_sequence = self._store.event_sequence(before_event_id)
            if before_sequence is None:
                return []
        return self._store.list_events(
            limit=min(limit, MAX_EVENT_PAGE), before_sequence=before_sequence
        )

    # ---- internals ------------------------------------------------------

    def _commit_selection(
        self,
        selection: InstallationSelection,
        *,
        previous_generation: int,
        action: str,
        profile_id: str | None = None,
        revision: int | None = None,
        previous_ref: str | None = None,
        next_ref: str | None = None,
        result: str = "recorded",
    ) -> InstallationSelection:
        """Write the one selection row and its audit event in one transaction.

        ``previous_generation`` makes the write a compare-and-swap on the
        generation the caller read. Its precondition checks ran against that
        generation, so if anything advanced it in between, the write is refused
        rather than silently applied on top of someone else's change.
        """
        actor = self.owner().owner_id
        with self._store.transaction() as conn:
            if not self._store.write_selection(
                conn, selection, previous_generation=previous_generation
            ):
                raise SelectionGenerationConflict(
                    "The installation selection changed while your change was being recorded: "
                    f"it is no longer generation {previous_generation}.",
                    next_step="Reload the selection and repeat your change.",
                )
            self._record_event(
                conn,
                actor=actor,
                action=action,
                profile_id=profile_id,
                revision=revision,
                previous_ref=previous_ref,
                next_ref=next_ref,
                result=result,
                recorded_at_ms=self._clock(),
            )
        return selection

    def _create_profile(
        self,
        *,
        display_name: str,
        credential_slot: str,
        endpoint_mode: EndpointMode,
        live_envelope: ValidatedLiveEnvelope | None,
        action: str,
        previous_ref: str | None,
    ) -> ProfileWithRevision:
        """One new profile and its revision 1, in one transaction.

        Creating and cloning differ only in where the content came from and
        what the audit event points back at, so they are one flow.
        """
        owner = self.owner()
        now = self._clock()
        profile = BrokerProfile(
            profile_id=new_identifier("profile"),
            owner_id=owner.owner_id,
            broker=ALPACA_BROKER,
            display_name=display_name,
            archived=False,
            created_at_ms=now,
            updated_at_ms=now,
        )
        revision = self._build_revision(
            profile_id=profile.profile_id,
            revision=1,
            credential_slot=credential_slot,
            endpoint_mode=endpoint_mode,
            live_envelope=live_envelope,
            author_owner_id=owner.owner_id,
            created_at_ms=now,
        )
        with _DisplayNameGuard(display_name), self._store.transaction() as conn:
            self._store.insert_profile(conn, profile)
            self._store.insert_revision(conn, revision)
            self._record_event(
                conn,
                actor=owner.owner_id,
                action=action,
                profile_id=profile.profile_id,
                revision=revision.revision,
                previous_ref=previous_ref,
                next_ref=revision.content_sha256,
                recorded_at_ms=now,
            )
        return ProfileWithRevision(profile=profile, latest_revision=revision)

    def _build_revision(
        self,
        *,
        profile_id: str,
        revision: int,
        credential_slot: str,
        endpoint_mode: EndpointMode,
        live_envelope: ValidatedLiveEnvelope | None,
        author_owner_id: str,
        created_at_ms: int,
    ) -> ProfileRevision:
        return ProfileRevision(
            profile_id=profile_id,
            revision=revision,
            schema_version=REVISION_SCHEMA_VERSION,
            credential_slot=credential_slot,
            endpoint_mode=endpoint_mode,
            account_pin=None,
            account_pinned_at_ms=None,
            live_envelope=live_envelope,
            content_sha256=revision_content_sha256(
                credential_slot=credential_slot,
                endpoint_mode=endpoint_mode,
                live_envelope=live_envelope,
            ),
            complete=_is_complete(
                credential_slot=credential_slot,
                endpoint_mode=endpoint_mode,
                live_envelope=live_envelope,
            ),
            author_owner_id=author_owner_id,
            created_at_ms=created_at_ms,
        )

    def _require_profile(self, profile_id: str) -> BrokerProfile:
        profile = self._store.read_profile(profile_id)
        if profile is None:
            raise ProfileNotFound(
                "That broker profile does not exist.",
                next_step="Reload the profile list and pick an existing profile.",
            )
        return profile

    def _read_bound_revision(self, profile_id: str, revision: int) -> ProfileRevision:
        """One revision, refusing an unknown profile before an unknown revision."""
        self._require_profile(profile_id)
        return self._require_revision(profile_id, revision)

    def _require_revision(self, profile_id: str, revision: int) -> ProfileRevision:
        stored = self._store.read_revision(profile_id, revision)
        if stored is None:
            raise RevisionNotFound(
                f"Revision {revision} of that profile does not exist.",
                next_step="Reload the revision history and pick an existing revision.",
            )
        return stored

    def _require_generation(self, current: InstallationSelection, expected: int) -> None:
        if expected != current.selection_generation:
            raise SelectionGenerationConflict(
                f"The installation selection changed while you were working: you expected "
                f"generation {expected}, the recorded generation is {current.selection_generation}.",
                next_step="Reload the selection and repeat your change.",
            )

    def _require_expected_revision(
        self, expected: int, latest: ProfileRevision | None
    ) -> None:
        recorded = 0 if latest is None else latest.revision
        if expected != recorded:
            raise RevisionConflict(
                f"This profile changed while you were editing it: you expected revision "
                f"{expected}, the saved revision is {recorded}.",
                next_step="Reload the profile and re-apply your change.",
            )

    def _refuse_archiving_profile_in_use(self, profile_id: str) -> None:
        selection = self._store.read_selection()
        if profile_id in (selection.staged_profile_id, selection.effective_profile_id):
            raise ProfileInUse(
                "That profile is staged or effective and cannot be archived.",
                next_step="Stage and apply a different profile first.",
            )

    def _record_event(
        self,
        conn: sqlite3.Connection,
        *,
        actor: str,
        action: str,
        recorded_at_ms: int,
        profile_id: str | None = None,
        revision: int | None = None,
        previous_ref: str | None = None,
        next_ref: str | None = None,
        result: str = "recorded",
    ) -> None:
        self._store.append_event(
            conn,
            ConfigurationEvent(
                event_id=new_identifier("event"),
                actor_owner_id=actor,
                action=action,
                profile_id=profile_id,
                revision=revision,
                previous_ref=previous_ref,
                next_ref=next_ref,
                result=result,
                recorded_at_ms=recorded_at_ms,
            ),
        )


class _DisplayNameGuard:
    """Translate the display-name unique index into its contract refusal.

    The rule that two live profiles cannot share a name lives in exactly one
    place — the partial unique index in ``schema.py``, as contract §5 requires
    ("uniqueness enforced in the schema rather than in application code"). A
    second copy of the rule in Python would be a check two concurrent creates
    could both pass; this only gives the index's ``IntegrityError`` the words
    the operator surface expects.
    """

    def __init__(self, display_name: str) -> None:
        self._display_name = display_name

    def __enter__(self) -> _DisplayNameGuard:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, _tb: object) -> bool:
        if isinstance(exc, sqlite3.IntegrityError) and _DISPLAY_NAME_CONSTRAINT in str(exc):
            raise DisplayNameConflict(
                f"Another profile is already named {self._display_name!r}.",
                next_step="Pick a different name, or archive the profile using that one.",
            ) from exc
        return False


__all__ = [
    "MAX_EVENT_PAGE",
    "BrokerConfigurationService",
    "revision_content_sha256",
]
