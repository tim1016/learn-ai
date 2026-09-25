"""A paper revision's own extended-hours allowances (#2440, owner decision 2026-09-25).

A regular-hours run's EXIT on the day's last bar reaches the broker after the
close as an after-hours limit priced off the decision bar's close less the exit
allowance, so a regular-hours run's Start refuses
``EXTENDED_HOURS_ALLOWANCE_UNSET`` until the account has one, and so does its
Resume when the run is flat (a run holding a position always resumes). Live seals the
pair in its envelope at arming; a paper revision had nowhere to hold it, which
would have stranded every regular-hours paper and ``sim:`` lane. These pin the
four things that change fixes:

* **Hash fidelity** (ADR 0060 Decision 6). Every revision saved before the pair
  existed hashes to the sha it was saved with — pinned as literals the earlier
  code produced, and re-derived from a real schema-v2 database after upgrade.
* **Storage.** Both or neither, paper only, never beside the envelope's own
  pair — in the service and again in the schema; the revision-immutability
  guard covers the new columns on a fresh and an upgraded database alike.
* **Validation at the boundary.** The same domain as the envelope's pair, and
  the four live-only values are refused rather than dropped.
* **Resolution.** An applied paper revision's pair reaches the pricing policy a
  paper Clerk and a ``sim:`` authority compose, so a regular-hours Start passes
  the extended-hours gate and the after-close EXIT is priced; without it the
  gate refuses with a next step that names the broker-profile field.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.broker.alpaca.active_binding import (
    reset_active_alpaca_binding_for_testing,
    set_active_alpaca_binding,
)
from app.broker.alpaca.broker import ALPACA_PAPER_CAPABILITIES
from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.clerk.program_leg import (
    EXTENDED_HOURS_ALLOWANCE_UNSET,
    ProgramLegPolicy,
    shape_program_leg,
)
from app.broker.alpaca.clerk.synthetic_broker import SYNTHETIC_CAPABILITIES
from app.broker.alpaca.config import reset_alpaca_settings_for_testing
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.broker.contract.capabilities import BrokerCapabilities
from app.broker.contract.models import OrderSide, OrderType
from app.broker_configuration import schema
from app.broker_configuration.alpaca_seams import AlpacaCredentialSlotDirectory
from app.broker_configuration.envelope import ValidatedLiveEnvelope, ValidatedPaperAllowances
from app.broker_configuration.errors import InvalidPaperAllowances
from app.broker_configuration.records import ProfileRevision
from app.broker_configuration.service import BrokerConfigurationService, revision_content_sha256
from app.broker_configuration.store import ProfilesStore, profiles_database_path
from app.broker_configuration.worker_binding import BoundWorker, resolve_worker_binding
from app.schemas.broker_configuration import PaperXhAllowancesPayload
from app.services.bot_start_admission import extended_hours_admission_fact
from app.services.source_bar_ledger import RetainedSourceBar
from app.utils.timestamps import to_ms_utc
from tests.broker.alpaca.profile.conftest import (
    DEFAULT_SLOT_KEY,
    DEFAULT_SLOT_SECRET,
    make_environment,
)
from tests.broker_configuration.conftest import (
    LIVE_ENVELOPE_PAYLOAD,
    OPERATOR_IDENTITY,
    FakeAccountVerifier,
    FrozenClock,
    slot_directory_for_tests,
)

# Distinct numbers, and not the envelope fixture's, so a passing assertion can
# only be explained by the paper pair having been read.
_PAIR = ValidatedPaperAllowances(xh_entry_bps=12.5, xh_exit_bps=7.25)

_V2_FIXTURE = Path(__file__).parents[1] / "fixtures" / "broker-configuration-profiles-v2.sql"
# What the pre-change code wrote as each fixture revision's ``content_sha256``,
# keyed by profile display name, and the live envelope's seal sha.
_V2_CONTENT_SHAS = {
    "Paper — testing": "7c62531f56ca43e13f94f3b66434ad30c1f7ac7d4e055ae045f581c58b4baa53",
    "Paper — imported": "4e0309dd055b688f0aa197e45c3783ab3fd497eee30d562b4674674a22dbec0b",
    "Live — primary": "6326c6f02675c2b119bce9b0c2203ba017050aa2f168145eb6eef9fa12b945af",
}
_V2_ENVELOPE_SHA = "df6c3a2cfb2409693c85fa6dff1b68f995e7154c8d70f8a6b6a12668835ebebf"

# Hashed by ``revision_content_sha256`` at 2627b117, before the pair existed.
_PINNED_ENVELOPE = ValidatedLiveEnvelope(
    loss_fraction=0.02,
    loss_usd=500.0,
    shadow_sessions=5,
    arming_max_sessions=20,
    xh_entry_bps=10.0,
    xh_exit_bps=12.5,
)
_PINNED_ENVELOPE_SHA = "df1202eb68eb93fb3b3b082283e533c68143e8e4e9209fccf93e54923ec7cd6d"

_ET = ZoneInfo("America/New_York")
_DAY = date(2026, 9, 2)  # a Wednesday with a full session


@pytest.fixture(autouse=True)
def _no_binding_leaks_between_tests() -> Iterator[None]:
    """The binding this resolution installs is process-wide; keep it per test."""
    reset_active_alpaca_binding_for_testing()
    reset_alpaca_settings_for_testing()
    yield
    reset_active_alpaca_binding_for_testing()
    reset_alpaca_settings_for_testing()


def _service(clerk_dir: Path, clock: FrozenClock) -> BrokerConfigurationService:
    return BrokerConfigurationService(
        store=ProfilesStore.open(clerk_dir=clerk_dir),
        operator_identity=OPERATOR_IDENTITY,
        worker_restart=None,
        clock=clock,
        credential_slots=slot_directory_for_tests(),
    )


def _paper_with_pair(service: BrokerConfigurationService) -> ProfileRevision:
    created = service.create_profile(
        display_name="Paper — regular hours",
        credential_slot="alpaca_paper_primary",
        endpoint_mode="paper",
        live_envelope=None,
        paper_xh_allowances=_PAIR,
    )
    assert created.latest_revision is not None
    return created.latest_revision


def _v2_database(clerk_dir: Path) -> None:
    """Materialise the committed schema-v2 database the pre-change code wrote."""
    path = profiles_database_path(clerk_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(_V2_FIXTURE.read_text(encoding="utf-8"))
    finally:
        connection.close()


# ---- hash fidelity ---------------------------------------------------------


@pytest.mark.parametrize(
    ("credential_slot", "endpoint_mode", "envelope", "pre_change_sha"),
    [
        ("default", "paper", None, "02d4e27a97704a452f395a34f8f46fde1708c866c8234fc83c1118da894409da"),
        ("live", "live", _PINNED_ENVELOPE, "8fd801455e4fb9e611e925cbd2b5504a8722df5f3b49806f57695a3ea6b73661"),
        # The legacy import's shape: a paper revision holding all six values.
        ("default", "paper", _PINNED_ENVELOPE, "39a4d1fb9519a3810636d15cdac9d73c4f422eabbfc8023a07ccd3c7471e96db"),
    ],
)
def test_a_revision_without_a_paper_pair_hashes_exactly_as_before_the_pair_existed(
    credential_slot: str,
    endpoint_mode: str,
    envelope: ValidatedLiveEnvelope | None,
    pre_change_sha: str,
) -> None:
    """Omitted when absent, never ``null``: the one rule that keeps every stored sha."""
    assert revision_content_sha256(
        credential_slot=credential_slot,
        endpoint_mode=endpoint_mode,  # type: ignore[arg-type]
        live_envelope=envelope,
        paper_xh_allowances=None,
    ) == pre_change_sha
    assert _PINNED_ENVELOPE.sha == _PINNED_ENVELOPE_SHA


def test_the_pair_is_part_of_the_content_a_revision_hashes() -> None:
    """Otherwise saving a pair onto an unchanged revision would be an idempotent no-op."""
    without = revision_content_sha256(
        credential_slot="default", endpoint_mode="paper", live_envelope=None
    )
    with_pair = revision_content_sha256(
        credential_slot="default", endpoint_mode="paper", live_envelope=None, paper_xh_allowances=_PAIR
    )
    other_pair = revision_content_sha256(
        credential_slot="default",
        endpoint_mode="paper",
        live_envelope=None,
        paper_xh_allowances=ValidatedPaperAllowances(xh_entry_bps=12.5, xh_exit_bps=7.5),
    )

    assert len({without, with_pair, other_pair}) == 3


def test_a_v2_database_upgrades_and_every_stored_revision_keeps_its_sha(
    clerk_dir: Path, clock: FrozenClock
) -> None:
    """Store -> load -> sha returns the sha the pre-change code wrote, row by row."""
    _v2_database(clerk_dir)

    service = _service(clerk_dir, clock)
    try:
        assert _stored_schema_version(clerk_dir) == schema.SCHEMA_VERSION == 3
        profiles = {profile.display_name: profile for profile in service.list_profiles()}
        assert set(profiles) == set(_V2_CONTENT_SHAS)
        for name, written_sha in _V2_CONTENT_SHAS.items():
            stored = service.read_revision(profiles[name].profile_id, 1)
            assert stored.paper_xh_allowances is None
            assert stored.content_sha256 == written_sha
            assert revision_content_sha256(
                credential_slot=stored.credential_slot,
                endpoint_mode=stored.endpoint_mode,
                live_envelope=stored.live_envelope,
                paper_xh_allowances=stored.paper_xh_allowances,
            ) == written_sha
        live = service.read_revision(profiles["Live — primary"].profile_id, 1)
        assert live.live_envelope is not None
        assert live.live_envelope.sha == _V2_ENVELOPE_SHA
        # The rest of the installation survives the upgrade too.
        assert service.selection().staged_profile_id == profiles["Paper — testing"].profile_id
        assert service.read_revision(profiles["Paper — testing"].profile_id, 1).account_pin == "PA000PAPER"

        # Re-saving unchanged content on an upgraded database is still a no-op.
        imported = profiles["Paper — imported"].profile_id
        stored = service.read_revision(imported, 1)
        resaved = service.create_revision(
            imported,
            expected_revision=1,
            credential_slot=stored.credential_slot,
            endpoint_mode=stored.endpoint_mode,
            live_envelope=stored.live_envelope,
        )
        assert resaved.revision == 1
    finally:
        service.close()


def test_an_upgraded_database_can_save_a_paper_pair(clerk_dir: Path, clock: FrozenClock) -> None:
    _v2_database(clerk_dir)
    service = _service(clerk_dir, clock)
    try:
        profile_id = next(
            profile.profile_id
            for profile in service.list_profiles()
            if profile.display_name == "Paper — testing"
        )
        saved = service.create_revision(
            profile_id,
            expected_revision=1,
            credential_slot="alpaca_paper_primary",
            endpoint_mode="paper",
            live_envelope=None,
            paper_xh_allowances=_PAIR,
        )

        assert saved.revision == 2
        assert service.read_revision(profile_id, 2).paper_xh_allowances == _PAIR
    finally:
        service.close()


def _stored_schema_version(clerk_dir: Path) -> int:
    connection = sqlite3.connect(profiles_database_path(clerk_dir))
    try:
        return int(connection.execute("SELECT schema_version FROM configuration_meta").fetchone()[0])
    finally:
        connection.close()


def _revision_table_shape(clerk_dir: Path) -> tuple[object, ...]:
    connection = sqlite3.connect(profiles_database_path(clerk_dir))
    try:
        columns = tuple(connection.execute("PRAGMA table_info(profile_revisions)").fetchall())
        guard = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'trg_profile_revisions_content_immutable'"
        ).fetchone()
    finally:
        connection.close()
    return columns, guard


def test_an_upgraded_database_and_a_fresh_one_have_the_same_revision_table(tmp_path: Path) -> None:
    """One column order, one guard: the upgrade and the fresh DDL cannot drift."""
    upgraded, fresh = tmp_path / "upgraded", tmp_path / "fresh"
    _v2_database(upgraded)
    ProfilesStore.open(clerk_dir=upgraded).close()
    ProfilesStore.open(clerk_dir=fresh).close()

    # Column affinities themselves are pinned with the envelope's, in
    # ``test_envelope_type_fidelity.py``.
    assert _revision_table_shape(upgraded) == _revision_table_shape(fresh)


# ---- storage ---------------------------------------------------------------


@pytest.fixture(params=["fresh", "upgraded"])
def any_database(
    request: pytest.FixtureRequest, clerk_dir: Path, clock: FrozenClock
) -> Iterator[BrokerConfigurationService]:
    """A service over a fresh database, or over the committed v2 one upgraded."""
    if request.param == "upgraded":
        _v2_database(clerk_dir)
    service = _service(clerk_dir, clock)
    yield service
    service.close()


@pytest.fixture
def raw_store(any_database: BrokerConfigurationService, clerk_dir: Path) -> Iterator[ProfilesStore]:
    """A second handle on the same database, for writes only the schema may refuse."""
    del any_database  # opened first, so the schema exists (and is upgraded) before this handle
    store = ProfilesStore.open(clerk_dir=clerk_dir)
    yield store
    store.close()


def test_a_paper_pair_round_trips_through_store_and_load(
    any_database: BrokerConfigurationService, clerk_dir: Path, clock: FrozenClock
) -> None:
    written = _paper_with_pair(any_database)
    any_database.close()

    reopened = _service(clerk_dir, clock)
    try:
        stored = reopened.read_revision(written.profile_id, written.revision)
    finally:
        reopened.close()

    assert stored == written
    assert stored.live_envelope is None
    assert stored.complete is True
    assert stored.paper_xh_allowances == _PAIR
    assert all(type(value) is float for value in stored.paper_xh_allowances.to_mapping().values())
    assert stored.content_sha256 == revision_content_sha256(
        credential_slot=stored.credential_slot,
        endpoint_mode="paper",
        live_envelope=None,
        paper_xh_allowances=_PAIR,
    )


def test_an_integer_allowance_is_stored_as_the_float_it_means(service: BrokerConfigurationService) -> None:
    """``5`` and ``5.0`` hash differently; the pair is widened once, on the way in."""
    created = service.create_profile(
        display_name="Paper — whole bps",
        credential_slot="alpaca_paper_primary",
        endpoint_mode="paper",
        live_envelope=None,
        paper_xh_allowances=ValidatedPaperAllowances.from_mapping({"xh_entry_bps": 5, "xh_exit_bps": 10}),
    )
    assert created.latest_revision is not None
    stored = service.read_revision(created.profile.profile_id, 1).paper_xh_allowances

    assert stored is not None
    assert stored.to_mapping() == {"xh_entry_bps": 5.0, "xh_exit_bps": 10.0}
    assert type(stored.xh_entry_bps) is float


def test_resaving_the_same_pair_is_a_no_op_and_changing_it_is_a_new_revision(
    service: BrokerConfigurationService,
) -> None:
    first = _paper_with_pair(service)

    again = service.create_revision(
        first.profile_id,
        expected_revision=1,
        credential_slot=first.credential_slot,
        endpoint_mode="paper",
        live_envelope=None,
        paper_xh_allowances=_PAIR,
    )
    cleared = service.create_revision(
        first.profile_id,
        expected_revision=1,
        credential_slot=first.credential_slot,
        endpoint_mode="paper",
        live_envelope=None,
    )

    assert again.revision == 1
    assert cleared.revision == 2
    assert cleared.paper_xh_allowances is None
    assert cleared.complete is True


def test_a_clone_carries_the_pair(service: BrokerConfigurationService) -> None:
    source = _paper_with_pair(service)

    cloned = service.clone_profile(source.profile_id, display_name="Paper — clone")

    assert cloned.latest_revision is not None
    assert cloned.latest_revision.paper_xh_allowances == _PAIR
    assert cloned.latest_revision.content_sha256 == source.content_sha256


def test_a_live_revision_cannot_carry_a_paper_pair(service: BrokerConfigurationService) -> None:
    """A live pair is the envelope's, sealed at arming; a second copy is refused."""
    with pytest.raises(InvalidPaperAllowances) as refused:
        service.create_profile(
            display_name="Live — two pairs",
            credential_slot="alpaca_live_primary",
            endpoint_mode="live",
            live_envelope=ValidatedLiveEnvelope.from_mapping(LIVE_ENVELOPE_PAYLOAD),
            paper_xh_allowances=_PAIR,
        )

    assert refused.value.reason == "paper_allowances_invalid"
    assert refused.value.next_step
    assert service.list_profiles() == []


def test_a_paper_revision_holding_the_six_live_values_cannot_also_carry_the_pair(
    service: BrokerConfigurationService,
) -> None:
    source = _paper_with_pair(service)

    with pytest.raises(InvalidPaperAllowances):
        service.create_revision(
            source.profile_id,
            expected_revision=1,
            credential_slot=source.credential_slot,
            endpoint_mode="paper",
            live_envelope=ValidatedLiveEnvelope.from_mapping(LIVE_ENVELOPE_PAYLOAD),
            paper_xh_allowances=_PAIR,
        )

    assert [revision.revision for revision in service.list_revisions(source.profile_id)] == [1]


def _raw_revision(base: ProfileRevision, **changes: object) -> ProfileRevision:
    """A revision written straight to the store, so only the schema can refuse it."""
    return replace(base, revision=base.revision + 1, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param(
            {"endpoint_mode": "live", "live_envelope": ValidatedLiveEnvelope.from_mapping(LIVE_ENVELOPE_PAYLOAD)},
            id="live-with-a-paper-pair",
        ),
        pytest.param(
            {"live_envelope": ValidatedLiveEnvelope.from_mapping(LIVE_ENVELOPE_PAYLOAD)},
            id="paper-with-six-values-and-a-pair",
        ),
        pytest.param({"endpoint_mode": "live", "live_envelope": None}, id="live-draft-with-a-pair"),
    ],
)
def test_the_schema_refuses_a_second_home_for_the_pair(
    any_database: BrokerConfigurationService, raw_store: ProfilesStore, changes: dict[str, object]
) -> None:
    base = _paper_with_pair(any_database)

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), raw_store.transaction() as conn:
        raw_store.insert_revision(conn, _raw_revision(base, **changes))


def test_the_schema_refuses_a_one_sided_pair(
    any_database: BrokerConfigurationService, raw_store: ProfilesStore
) -> None:
    base = _paper_with_pair(any_database)

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), raw_store.transaction() as conn:
        conn.execute(
            "INSERT INTO profile_revisions (profile_id, revision, schema_version, credential_slot, "
            "endpoint_mode, paper_xh_entry_bps, content_sha256, complete, author_owner_id, "
            "created_at_ms) VALUES (?, 2, 1, 'alpaca_paper_primary', 'paper', 5.0, 'x', 1, ?, 1)",
            (base.profile_id, base.author_owner_id),
        )


@pytest.mark.parametrize("column", ["paper_xh_entry_bps", "paper_xh_exit_bps"])
def test_the_immutability_guard_covers_the_pair(
    any_database: BrokerConfigurationService, raw_store: ProfilesStore, column: str
) -> None:
    """Unpinned, so only the content guard — not the pin clause — can refuse it."""
    written = _paper_with_pair(any_database)
    assert written.account_pin is None

    with pytest.raises(sqlite3.IntegrityError, match="immutable"), raw_store.transaction() as conn:
        conn.execute(
            f"UPDATE profile_revisions SET {column} = 99.0 WHERE profile_id = ? AND revision = ?",
            (written.profile_id, written.revision),
        )


# ---- validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "mapping",
    [
        pytest.param({"xh_exit_bps": 7.25}, id="entry-missing"),
        pytest.param({"xh_entry_bps": 12.5}, id="exit-missing"),
        pytest.param({**_PAIR.to_mapping(), "loss_usd": 500.0}, id="a-live-only-value"),
        pytest.param({**_PAIR.to_mapping(), "shadow_sessions": 3}, id="a-session-count"),
        pytest.param({**_PAIR.to_mapping(), "xh_exit_bps": True}, id="a-boolean"),
        pytest.param({**_PAIR.to_mapping(), "xh_exit_bps": "7.25"}, id="a-string"),
        pytest.param({**_PAIR.to_mapping(), "xh_exit_bps": Decimal("7.25")}, id="a-decimal"),
        pytest.param({**_PAIR.to_mapping(), "xh_exit_bps": float("inf")}, id="infinite"),
        pytest.param({**_PAIR.to_mapping(), "xh_exit_bps": float("nan")}, id="nan"),
        pytest.param({**_PAIR.to_mapping(), "xh_entry_bps": -0.5}, id="negative"),
        pytest.param({**_PAIR.to_mapping(), "xh_exit_bps": 10_000.0}, id="one-hundred-percent"),
    ],
)
def test_the_validated_pair_refuses_what_the_envelope_pair_refuses(mapping: dict[str, object]) -> None:
    with pytest.raises(InvalidPaperAllowances):
        ValidatedPaperAllowances.from_mapping(mapping)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({**_PAIR.to_mapping(), "xh_exit_bps": True}, id="a-boolean"),
        pytest.param({**_PAIR.to_mapping(), "xh_exit_bps": "7.25"}, id="a-string"),
        pytest.param({**_PAIR.to_mapping(), "loss_usd": 500.0}, id="a-live-only-value"),
        pytest.param({**_PAIR.to_mapping(), "xh_exit_bps": 10_000.0}, id="one-hundred-percent"),
        pytest.param({**_PAIR.to_mapping(), "xh_entry_bps": -0.5}, id="negative"),
        pytest.param({**_PAIR.to_mapping(), "xh_exit_bps": float("inf")}, id="infinite"),
        pytest.param({"xh_exit_bps": 7.25}, id="entry-missing"),
    ],
)
def test_the_request_dto_refuses_before_the_validated_type_sees_it(payload: dict[str, object]) -> None:
    """Strict, closed and bounded, like ``LiveEnvelopePayload`` (lax mode once took ``true``)."""
    with pytest.raises(ValidationError):
        PaperXhAllowancesPayload(**payload)  # type: ignore[arg-type]


def test_the_request_dto_widens_an_integer_exactly_as_the_validated_type_does() -> None:
    payload = PaperXhAllowancesPayload(xh_entry_bps=5, xh_exit_bps=10)

    assert ValidatedPaperAllowances.from_mapping(payload.model_dump()) == (
        ValidatedPaperAllowances.from_mapping({"xh_entry_bps": 5, "xh_exit_bps": 10})
    )
    assert type(payload.xh_entry_bps) is float


# ---- resolution: an applied paper revision prices the after-close EXIT ----


class _ReadPort:
    """Only what ``ProgramLegPolicy.from_read_port`` asks of a read port."""

    def __init__(self, capabilities: BrokerCapabilities) -> None:
        self._capabilities = capabilities

    def capabilities(self) -> BrokerCapabilities:
        return self._capabilities


def _bar(hour: int, minute: int, *, close: str = "100.00") -> RetainedSourceBar:
    end = to_ms_utc(datetime(_DAY.year, _DAY.month, _DAY.day, hour, minute, tzinfo=_ET))
    return RetainedSourceBar(
        seq=1,
        account_id="PA000PAPER",
        provider="alpaca",
        symbol="SPY",
        bar_identity=f"alpaca:SPY:{end - 60_000}:{end}",
        bar_ref="bar-1",
        start_ms=end - 60_000,
        end_ms=end,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=1,
        fetched_at_ms=end,
        session_phase="UNKNOWN",
    )


async def _apply_and_bind(
    clerk_dir: Path, clock: FrozenClock, *, pair: ValidatedPaperAllowances | None
) -> BoundWorker:
    """Save, stage and Apply one paper revision, then boot the worker on it.

    The real startup ceremony against the real slot directory — the one path
    by which a revision reaches a running Clerk, and only on an explicit Apply.
    """
    environment = make_environment(api_key_id=DEFAULT_SLOT_KEY, api_secret_key=DEFAULT_SLOT_SECRET)
    service = BrokerConfigurationService(
        store=ProfilesStore.open(clerk_dir=clerk_dir),
        operator_identity=OPERATOR_IDENTITY,
        worker_restart=None,
        clock=clock,
        credential_slots=AlpacaCredentialSlotDirectory(environment=environment),
        account_verifier=FakeAccountVerifier(),
    )
    try:
        created = service.create_profile(
            display_name="Paper — regular hours",
            credential_slot="default",
            endpoint_mode="paper",
            live_envelope=None,
            paper_xh_allowances=pair,
        )
        service.stage_selection(
            profile_id=created.profile.profile_id, revision=1, expected_selection_generation=0
        )
        service.request_apply(expected_selection_generation=service.selection().selection_generation)

        resolved = await resolve_worker_binding(service_factory=lambda: service, environment=environment)
    finally:
        service.close()
    assert isinstance(resolved, BoundWorker)
    set_active_alpaca_binding(resolved.context)
    return resolved


@pytest.mark.parametrize(
    "capabilities",
    [
        pytest.param(ALPACA_PAPER_CAPABILITIES, id="paper-clerk"),
        pytest.param(SYNTHETIC_CAPABILITIES, id="sim-authority"),
    ],
)
async def test_an_applied_paper_pair_admits_a_regular_hours_run_and_prices_its_last_exit(
    clerk_dir: Path, clock: FrozenClock, capabilities: BrokerCapabilities
) -> None:
    bound = await _apply_and_bind(clerk_dir, clock, pair=_PAIR)
    # A paper binding never composes a live envelope out of its allowances.
    assert bound.context.live_envelope is None
    assert bound.context.settings.is_paper

    # The same composition a paper Clerk and a sim: authority run.
    policy = ProgramLegPolicy.from_read_port(_ReadPort(capabilities))
    assert policy.allowances == ExtendedHoursAllowances(
        entry_bps=Decimal("12.5"), exit_bps=Decimal("7.25")
    )
    admission = extended_hours_admission_fact(use_rth=True, policy=policy, observed_at_ms=1_000)
    assert admission.state == "NOT_REQUESTED"

    # The 15:59-16:00 bar is decided at 16:00: after the close, so the EXIT is
    # an after-hours limit 7.25 bps under the close — floored to the tick.
    sell = shape_program_leg(
        side=OrderSide.SELL,
        purpose=EffectPurpose.EXIT,
        use_rth=True,
        decision_bar=_bar(16, 0),
        policy=policy,
    )
    cover = shape_program_leg(
        side=OrderSide.BUY,
        purpose=EffectPurpose.EXIT,
        use_rth=True,
        decision_bar=_bar(16, 0),
        policy=policy,
    )

    assert sell.unpriced is None
    assert sell.shape.order_type is OrderType.LIMIT
    assert sell.shape.extended_hours is True
    assert sell.shape.limit_price == pytest.approx(99.92, abs=1e-9)  # floor(99.9275)
    assert cover.shape.limit_price == pytest.approx(100.08, abs=1e-9)  # ceil(100.0725)


async def test_without_the_pair_the_gate_refuses_and_names_the_profile_field(
    clerk_dir: Path, clock: FrozenClock
) -> None:
    await _apply_and_bind(clerk_dir, clock, pair=None)

    policy = ProgramLegPolicy.from_read_port(_ReadPort(ALPACA_PAPER_CAPABILITIES))
    admission = extended_hours_admission_fact(use_rth=True, policy=policy, observed_at_ms=1_000)
    leg = shape_program_leg(
        side=OrderSide.SELL,
        purpose=EffectPurpose.EXIT,
        use_rth=True,
        decision_bar=_bar(16, 0),
        policy=policy,
    )

    assert policy.allowances is None
    # A regular-hours run's own state: Start refuses it with the shared refusal.
    assert admission.state == "EXIT_ALLOWANCE_UNSET"
    assert leg.unpriced == EXTENDED_HOURS_ALLOWANCE_UNSET
    # The operator is sent to the field that fixes it, on the page that has it.
    next_step = EXTENDED_HOURS_ALLOWANCE_UNSET.next_step
    assert "broker configuration page" in next_step
    assert "paper revision has its own two fields" in next_step
    assert "extended-hours offsets" in next_step
