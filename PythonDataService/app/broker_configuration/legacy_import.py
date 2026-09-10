"""One-time import of an environment-configured installation into a profile.

The cutover half of package F. An installation configured the ADR 0059 way — the
endpoint mode and the six risk-envelope values in ``PythonDataService/.env`` —
runs this once to get a saved profile holding exactly those values, after which
``worker_binding`` binds the profile instead of bootstrapping from the
environment and the retired lines can be deleted.

**Plan then apply**, the shape ``clerk/sqlite/cutover.py`` invented and
``clerk/ceremony.py`` now shares: a read-only plan whose content hash *is* its
confirmation token, a bounded confirmation window, and an apply that re-reads
everything and refuses on drift. Two properties that buys here specifically:

- The operator sees the six numbers that will bound real money **before** they
  are written anywhere, and confirms that exact document. The plan's assignment
  says "never guess a live limit"; quoting the token is how the operator says
  they read it.
- An ``.env`` edited between preview and apply is a refusal, not a silent import
  of different values.

What this deliberately does **not** do:

- **It never pins an account.** Pinning is read-only broker discovery followed by
  an operator choosing from what was observed (contract §3), and that seam is the
  configuration page's. An importer that pinned would be a second seam, and
  "never silently import a different account" is exactly the failure it would
  risk. The imported revision is unpinned, which is bindable — the worker
  re-observes at startup — and the plan says so.
- **It never records an Apply.** Apply is the operator's explicit act (ADR 0060
  Decision 4) and it is pressed on the configuration page. Importing stages at
  most; staging governs nothing.
- **It never touches a secret.** The revision references a credential *slot*; the
  legacy key pair stays exactly where it is and keeps working, because it *is*
  the ``default`` slot.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Final

from app.broker.alpaca.clerk.ceremony import (
    DEFAULT_CONFIRMATION_TTL_MS,
    plan_content_token,
    plan_payload,
    require_confirmation_ttl_ms,
    require_plan_token,
    require_unexpired,
)
from app.broker.alpaca.profile import (
    CREDENTIAL_SLOT_DEFAULT,
    describe_credential_slots,
    is_known_credential_slot,
)
from app.broker_configuration.envelope import ENVELOPE_FIELDS, ValidatedLiveEnvelope
from app.broker_configuration.errors import InvalidLiveEnvelope
from app.broker_configuration.legacy_environment import (
    ENVELOPE_FIELD_BY_SETTING,
    RETIRED_ENV_VARS,
    LegacyEnvironmentValues,
)
from app.broker_configuration.records import EndpointMode, ProfileRevision
from app.broker_configuration.service import BrokerConfigurationService, revision_content_sha256

IMPORT_PLAN_SCHEMA_VERSION: Final = 1

DEFAULT_IMPORT_DISPLAY_NAME: Final = "Imported from environment"

_LABEL: Final = "configuration import"


class ConfigurationImportRefused(Exception):
    """This import cannot be planned or applied as asked.

    One operator-readable sentence, following ``CutoverRefused``: this ceremony
    has no reason-code vocabulary of its own, and every refusal it raises is
    something the operator fixes in the environment or on the command line.
    """


@dataclass(frozen=True)
class ExistingProfile:
    """One profile already in the database, as the plan needs to see it."""

    profile_id: str
    display_name: str
    archived: bool
    latest_revision: int | None
    latest_content_sha256: str | None


@dataclass(frozen=True)
class ExistingConfiguration:
    """What the profiles database already holds — read without writing to it.

    ``owner_display_label`` is ``None`` when no owner row exists yet, which is
    why :meth:`BrokerConfigurationService.existing_owner` exists: asking through
    ``owner()`` would create the record while answering the question.
    """

    owner_display_label: str | None
    profiles: tuple[ExistingProfile, ...]
    selection_generation: int
    available_slots: frozenset[str]

    @classmethod
    def absent(cls) -> ExistingConfiguration:
        """An installation whose profiles database does not exist yet.

        The plan is built against this rather than against a freshly-created
        empty database, so a preview does not bring one into being. Slot
        availability is still read: it comes from the environment, not the
        database.
        """
        return cls(
            owner_display_label=None,
            profiles=(),
            selection_generation=0,
            available_slots=_available_slots(),
        )

    @classmethod
    def read(cls, service: BrokerConfigurationService) -> ExistingConfiguration:
        """Read an existing database. Every call here is a read."""
        owner = service.existing_owner()
        profiles: list[ExistingProfile] = []
        for profile in service.list_profiles(include_archived=True):
            revisions = service.list_revisions(profile.profile_id)
            latest = max(revisions, key=lambda item: item.revision, default=None)
            profiles.append(
                ExistingProfile(
                    profile_id=profile.profile_id,
                    display_name=profile.display_name,
                    archived=profile.archived,
                    latest_revision=None if latest is None else latest.revision,
                    latest_content_sha256=None if latest is None else latest.content_sha256,
                )
            )
        return cls(
            owner_display_label=None if owner is None else owner.display_label,
            profiles=tuple(profiles),
            selection_generation=service.selection().selection_generation,
            available_slots=_available_slots(),
        )


@dataclass(frozen=True)
class ImportedProfileRef:
    """A profile already holding exactly the content this import would write."""

    profile_id: str
    revision: int


@dataclass(frozen=True)
class ImportPlan:
    """What the import would write, and the evidence it was read, not guessed.

    ``plan_id`` and ``confirmation_token`` are both the sha256 of everything
    below them, so a plan file whose numbers were edited no longer verifies.
    """

    schema_version: int
    plan_id: str
    confirmation_token: str
    created_at_ms: int
    expires_at_ms: int
    display_name: str
    credential_slot: str
    credential_slot_available: bool
    endpoint_mode: EndpointMode
    live_envelope: ValidatedLiveEnvelope | None
    envelope_sha: str | None
    revision_content_sha256: str
    owner_display_label: str
    owner_will_be_created: bool
    source_variables: tuple[str, ...]
    stage_selection: bool
    expected_selection_generation: int
    already_imported: ImportedProfileRef | None
    notes: tuple[str, ...]

    @property
    def writes_anything(self) -> bool:
        """Whether applying this plan would create a profile revision."""
        return self.already_imported is None


@dataclass(frozen=True)
class ImportReceipt:
    """What the apply actually did."""

    profile_id: str
    revision: int
    created: bool
    """``False`` when the content was already present — a repeated import."""

    staged: bool
    owner_display_label: str
    envelope_sha: str | None
    notes: tuple[str, ...]


def _refused(message: str) -> Exception:
    return ConfigurationImportRefused(message)


def _available_slots() -> frozenset[str]:
    """Which credential slots have their pair injected. Names and booleans only."""
    return frozenset(slot.slot for slot in describe_credential_slots() if slot.available)


def _envelope_from(values: LegacyEnvironmentValues) -> ValidatedLiveEnvelope | None:
    """The six values as one validated envelope, or ``None`` when incomplete.

    Returns ``None`` rather than a partial object: a live envelope is all six
    values or it is not one, and inventing a default for a missing limit is the
    single thing this tool must never do.
    """
    supplied = {
        envelope_field: getattr(values, settings_field)
        for settings_field, envelope_field in ENVELOPE_FIELD_BY_SETTING.items()
    }
    if any(value is None for value in supplied.values()):
        return None
    return ValidatedLiveEnvelope.from_mapping(supplied)


def _missing_envelope_variables(values: LegacyEnvironmentValues) -> tuple[str, ...]:
    """Which ``ALPACA_LIVE_*`` variables have no value, in declaration order."""
    return tuple(
        f"ALPACA_{settings_field.upper()}"
        for settings_field in ENVELOPE_FIELD_BY_SETTING
        if getattr(values, settings_field) is None
    )


def _present_source_variables(values: LegacyEnvironmentValues) -> tuple[str, ...]:
    """Which retired variables actually contributed a value to this plan."""
    present = {
        f"ALPACA_{field.upper()}"
        for field in ("mode", *ENVELOPE_FIELD_BY_SETTING)
        if getattr(values, field) is not None
    }
    return tuple(name for name in RETIRED_ENV_VARS if name in present)


def plan_import(
    *,
    existing: ExistingConfiguration,
    values: LegacyEnvironmentValues,
    display_name: str = DEFAULT_IMPORT_DISPLAY_NAME,
    credential_slot: str = CREDENTIAL_SLOT_DEFAULT,
    operator_identity: str,
    stage_selection: bool = True,
    confirmation_ttl_ms: int = DEFAULT_CONFIRMATION_TTL_MS,
    now_ms: int,
) -> ImportPlan:
    """Read the environment and the database, and describe what would be written.

    Pure over its inputs and writes nothing — the reads are the caller's, so a
    preview cannot create the profiles database by asking it a question.
    """
    ttl = require_confirmation_ttl_ms(confirmation_ttl_ms, refused=_refused)
    if not is_known_credential_slot(credential_slot):
        raise _refused(
            f"credential slot {credential_slot!r} is not on the allowlist; "
            "a slot name is never used to build a variable name"
        )
    if not display_name.strip():
        raise _refused("the imported profile needs a display name")

    endpoint_mode: EndpointMode = values.mode or "paper"
    envelope = _envelope_from(values)
    missing = _missing_envelope_variables(values)
    notes: list[str] = []

    if endpoint_mode == "live" and envelope is None:
        # The environment says live but cannot say with what limits. Refusing is
        # the whole point: `AlpacaSettings._enforce_mode_agreement` refuses the
        # same state at boot, and importing it would manufacture a live profile
        # bounded by numbers nobody chose.
        raise _refused(
            "ALPACA_MODE=live but the live envelope is incomplete; missing "
            + ", ".join(missing)
            + ". Complete the environment or import as paper — this tool will not "
            "supply a live limit."
        )

    source_variables = _present_source_variables(values)
    if not source_variables:
        raise _refused(
            "no retired Alpaca setting is present in the environment, so there is "
            "nothing to import. Create the profile on the configuration page instead."
        )

    if envelope is None and missing:
        notes.append(
            "The imported revision carries no live envelope: "
            + ", ".join(missing)
            + " "
            + ("is" if len(missing) == 1 else "are")
            + " absent. A live revision needs all six values."
        )
    elif endpoint_mode == "paper" and envelope is not None:
        notes.append(
            "The environment's six live values are preserved on this paper "
            "revision, so switching it to live later does not lose them. They "
            "bound nothing while the endpoint mode is paper."
        )

    notes.append(
        "The imported revision names no broker account. Verify and approve its "
        "account on the configuration page before pressing Apply; this tool "
        "never observes or pins an account."
    )

    content_sha256 = revision_content_sha256(
        credential_slot=credential_slot,
        endpoint_mode=endpoint_mode,
        live_envelope=envelope,
    )
    already = _already_imported(existing, content_sha256)
    if already is None:
        _require_display_name_free(existing, display_name)
    else:
        notes.append(
            "These exact values are already saved as revision "
            f"{already.revision} of profile {already.profile_id}; applying this "
            "plan creates nothing."
        )

    if credential_slot not in existing.available_slots:
        notes.append(
            f"Credential slot {credential_slot!r} has no injected pair right now. "
            "The revision saves fine; binding it will refuse until the pair is "
            "present."
        )

    draft = ImportPlan(
        schema_version=IMPORT_PLAN_SCHEMA_VERSION,
        plan_id="",
        confirmation_token="",
        created_at_ms=now_ms,
        expires_at_ms=now_ms + ttl,
        display_name=display_name,
        credential_slot=credential_slot,
        credential_slot_available=credential_slot in existing.available_slots,
        endpoint_mode=endpoint_mode,
        live_envelope=envelope,
        envelope_sha=None if envelope is None else envelope.sha,
        revision_content_sha256=content_sha256,
        owner_display_label=existing.owner_display_label or operator_identity,
        owner_will_be_created=existing.owner_display_label is None,
        source_variables=source_variables,
        stage_selection=stage_selection,
        expected_selection_generation=existing.selection_generation,
        already_imported=already,
        notes=tuple(notes),
    )
    token = plan_content_token(_import_plan_payload(draft))
    return replace(draft, plan_id=token, confirmation_token=token)


def _import_plan_payload(plan: ImportPlan) -> dict[str, object]:
    return plan_payload(
        plan,
        schema_version=IMPORT_PLAN_SCHEMA_VERSION,
        refused=_refused,
        label=_LABEL,
    )


def _already_imported(
    existing: ExistingConfiguration, content_sha256: str
) -> ImportedProfileRef | None:
    """A profile whose latest revision already holds this exact content.

    Matched on content, not on display name: if the operator already saved these
    values by hand under another name, importing must adopt that profile rather
    than mint a near-duplicate. Archived profiles are searched too — an archived
    profile still holds the content, and creating a second copy of it is not what
    "idempotent" means.
    """
    for profile in existing.profiles:
        if profile.latest_content_sha256 == content_sha256 and profile.latest_revision is not None:
            return ImportedProfileRef(
                profile_id=profile.profile_id, revision=profile.latest_revision
            )
    return None


def _require_display_name_free(existing: ExistingConfiguration, display_name: str) -> None:
    for profile in existing.profiles:
        if not profile.archived and profile.display_name == display_name:
            raise _refused(
                f"a profile named {display_name!r} already exists with different "
                "content; choose another name with --display-name"
            )


def apply_import(
    *,
    plan: ImportPlan,
    confirmation_token: str,
    service: BrokerConfigurationService,
    values: LegacyEnvironmentValues,
    now_ms: int,
) -> ImportReceipt:
    """Write the planned revision, after re-checking that the plan still holds.

    The order of the two guards below is deliberate and load-bearing. **Already
    imported is checked before environment drift**, because the documented
    cutover order is import, then delete the retired lines, then restart — so a
    second run after the deletion sees an environment that no longer matches the
    plan. Treating that as drift would make "repeated import is idempotent" false
    exactly when an operator re-runs to confirm the first one worked.
    """
    require_plan_token(
        _import_plan_payload(plan),
        plan_id=plan.plan_id,
        confirmation_token=plan.confirmation_token,
        supplied_token=confirmation_token,
        refused=_refused,
        label=_LABEL,
    )
    require_unexpired(
        now_ms=now_ms, expires_at_ms=plan.expires_at_ms, refused=_refused, label=_LABEL
    )

    existing = ExistingConfiguration.read(service)
    already = _already_imported(existing, plan.revision_content_sha256)
    if already is not None:
        owner = service.owner()
        staged = _stage(service, already, plan=plan, existing=existing)
        return ImportReceipt(
            profile_id=already.profile_id,
            revision=already.revision,
            created=False,
            staged=staged,
            owner_display_label=owner.display_label,
            envelope_sha=plan.envelope_sha,
            notes=("These values were already saved; no revision was created.",),
        )

    _require_environment_unchanged(plan, values)
    _require_display_name_free(existing, plan.display_name)

    # Reads the owner first, which creates it when absent, seeded from
    # ``PANEL_OPERATOR_IDENTITY``. An existing label is never overwritten:
    # importing operator identity must not rewrite history (plan §F), and a
    # rename is a separate, explicit act.
    owner = service.owner()
    created = service.create_profile(
        display_name=plan.display_name,
        credential_slot=plan.credential_slot,
        endpoint_mode=plan.endpoint_mode,
        live_envelope=plan.live_envelope,
    )
    revision = created.latest_revision
    if revision is None:  # pragma: no cover - create_profile always writes revision 1
        raise _refused("the imported profile was created without a revision")
    _require_envelope_fidelity(plan, revision)

    reference = ImportedProfileRef(profile_id=created.profile.profile_id, revision=revision.revision)
    staged = _stage(service, reference, plan=plan, existing=existing)
    return ImportReceipt(
        profile_id=reference.profile_id,
        revision=reference.revision,
        created=True,
        staged=staged,
        owner_display_label=owner.display_label,
        envelope_sha=plan.envelope_sha,
        notes=plan.notes,
    )


def _stage(
    service: BrokerConfigurationService,
    reference: ImportedProfileRef,
    *,
    plan: ImportPlan,
    existing: ExistingConfiguration,
) -> bool:
    """Stage the imported revision, or leave the selection alone.

    Staging governs nothing (ADR 0060 Decision 4) — it records what the operator
    picked, and the Apply that makes it effective is pressed on the configuration
    page. The generation fence is carried from the *plan*, so a selection changed
    since the preview conflicts rather than being overwritten.
    """
    if not plan.stage_selection:
        return False
    service.stage_selection(
        profile_id=reference.profile_id,
        revision=reference.revision,
        expected_selection_generation=existing.selection_generation,
    )
    return True


def _require_environment_unchanged(plan: ImportPlan, values: LegacyEnvironmentValues) -> None:
    """Refuse when the environment no longer says what the plan quoted.

    The operator confirmed a specific set of numbers. An ``.env`` edited in the
    confirmation window must not be imported under a token that describes the
    previous ones.
    """
    current_mode: EndpointMode = values.mode or "paper"
    current_envelope = _envelope_from(values)
    if current_mode != plan.endpoint_mode:
        raise _refused(
            "the environment's endpoint mode changed after planning "
            f"({plan.endpoint_mode!r} → {current_mode!r}); re-run the plan"
        )
    if current_envelope != plan.live_envelope:
        raise _refused(
            "the environment's live envelope values changed after planning; re-run the plan"
        )


def _require_envelope_fidelity(plan: ImportPlan, revision: ProfileRevision) -> None:
    """The stored revision must reproduce the legacy envelope's ``sha``, bit for bit.

    ADR 0060 Decision 6's obligation, asserted on the one path that carries a
    live limit out of the environment and into storage. A ``5000`` stored where
    the environment held ``5000.0`` is a different envelope document, and every
    arming record already in the operator's ledger is sealed over the latter.
    """
    stored = revision.live_envelope
    if (stored is None) != (plan.live_envelope is None):
        raise _refused("the imported revision's live envelope does not match the plan")
    if stored is None or plan.live_envelope is None:
        return
    if stored.sha != plan.envelope_sha:
        raise _refused(
            "the imported revision's live envelope hashes differently from the "
            "environment it came from; the import was rolled back"
        )
    for field in ENVELOPE_FIELDS:
        planned = getattr(plan.live_envelope, field)
        written = getattr(stored, field)
        if type(planned) is not type(written) or planned != written:
            raise InvalidLiveEnvelope(
                f"{field} did not round-trip through storage with its original type."
            )


__all__ = [
    "DEFAULT_IMPORT_DISPLAY_NAME",
    "IMPORT_PLAN_SCHEMA_VERSION",
    "ConfigurationImportRefused",
    "ExistingConfiguration",
    "ExistingProfile",
    "ImportPlan",
    "ImportReceipt",
    "ImportedProfileRef",
    "apply_import",
    "plan_import",
]
