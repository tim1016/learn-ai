-- A schema_version=2 broker-configuration profiles database, as the code
-- before #2440's paper allowances wrote it. The v2 -> v3 upgrade and the
-- revision content-hash fidelity test read it:
-- tests/broker_configuration/test_paper_extended_hours_allowances.py.
--
-- Generated once at 2627b117 (fix/2440-after-close-exit-ah-limit) by driving
-- BrokerConfigurationService over a tmp Clerk directory with the package's
-- test fixtures (FrozenClock at 1_757_000_000_000, advanced 1 s per step;
-- slot_directory_for_tests(); LIVE_ENVELOPE_PAYLOAD), then
-- sqlite3.Connection.iterdump() after a WAL checkpoint. Three revisions:
--   * "Paper — testing"  paper, no envelope, pinned to PA000PAPER and staged;
--   * "Paper — imported" paper carrying all six live values (the shape the
--     legacy import writes for a paper environment that held them);
--   * "Live — primary"   live, all six live values.
-- Never hand-edited: every content_sha256 below is the hash that code wrote,
-- and the test asserts today's code reproduces it after the upgrade.
BEGIN TRANSACTION;
CREATE TABLE account_nicknames (
    account_id          TEXT PRIMARY KEY,
    nickname            TEXT NOT NULL CHECK (length(nickname) > 0),
    updated_at_ms       INTEGER NOT NULL
);
CREATE TABLE broker_profiles (
    profile_id          TEXT PRIMARY KEY,
    owner_id            TEXT NOT NULL REFERENCES local_owner(owner_id),
    -- No CHECK pinning 'alpaca'. Contract §2.2's whole reason for this column
    -- is that a second broker should not need a migration, and changing a
    -- SQLite CHECK means rebuilding the table. The closed set lives in the
    -- request DTO's Literal, where widening it is a one-line change.
    broker              TEXT NOT NULL CHECK (length(broker) > 0),
    display_name        TEXT NOT NULL CHECK (length(display_name) > 0),
    archived            INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    created_at_ms       INTEGER NOT NULL,
    updated_at_ms       INTEGER NOT NULL
);
INSERT INTO "broker_profiles" VALUES('profile_fa686d23bb2d1208','owner_017507bde12844c0','alpaca','Paper — testing',0,1757000000000,1757000000000);
INSERT INTO "broker_profiles" VALUES('profile_00212df693131ff7','owner_017507bde12844c0','alpaca','Paper — imported',0,1757000001000,1757000001000);
INSERT INTO "broker_profiles" VALUES('profile_ed16a6b5efa77d0d','owner_017507bde12844c0','alpaca','Live — primary',0,1757000002000,1757000002000);
CREATE TABLE configuration_events (
    event_id            TEXT PRIMARY KEY,
    sequence            INTEGER NOT NULL,
    actor_owner_id      TEXT NOT NULL REFERENCES local_owner(owner_id),
    action              TEXT NOT NULL,
    profile_id          TEXT,
    revision            INTEGER,
    previous_ref        TEXT,
    next_ref            TEXT,
    result              TEXT NOT NULL,
    recorded_at_ms      INTEGER NOT NULL
);
INSERT INTO "configuration_events" VALUES('event_d03273dd6f5807f4',1,'owner_017507bde12844c0','profile_created','profile_fa686d23bb2d1208',1,NULL,'7c62531f56ca43e13f94f3b66434ad30c1f7ac7d4e055ae045f581c58b4baa53','recorded',1757000000000);
INSERT INTO "configuration_events" VALUES('event_cb53c57bef66aa81',2,'owner_017507bde12844c0','profile_created','profile_00212df693131ff7',1,NULL,'4e0309dd055b688f0aa197e45c3783ab3fd497eee30d562b4674674a22dbec0b','recorded',1757000001000);
INSERT INTO "configuration_events" VALUES('event_b23c22a16c686392',3,'owner_017507bde12844c0','profile_created','profile_ed16a6b5efa77d0d',1,NULL,'6326c6f02675c2b119bce9b0c2203ba017050aa2f168145eb6eef9fa12b945af','recorded',1757000002000);
INSERT INTO "configuration_events" VALUES('event_561c986cf36413a4',4,'owner_017507bde12844c0','account_pinned','profile_fa686d23bb2d1208',1,NULL,'PA000PAPER','recorded',1757000003000);
INSERT INTO "configuration_events" VALUES('event_2c57c144f80d326b',5,'owner_017507bde12844c0','selection_staged','profile_fa686d23bb2d1208',1,NULL,'generation:1','recorded',1757000004000);
CREATE TABLE configuration_meta (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version      INTEGER NOT NULL,
    created_at_ms       INTEGER NOT NULL,
    updated_at_ms       INTEGER NOT NULL
);
INSERT INTO "configuration_meta" VALUES(1,2,1790342873115,1790342873115);
CREATE TABLE installation_selection (
    id                          INTEGER PRIMARY KEY CHECK (id = 1),
    staged_profile_id           TEXT,
    staged_revision             INTEGER,
    apply_requested             INTEGER NOT NULL DEFAULT 0 CHECK (apply_requested IN (0, 1)),
    apply_requested_at_ms       INTEGER,
    apply_requested_generation  INTEGER,
    selection_generation        INTEGER NOT NULL CHECK (selection_generation >= 0),
    -- D9 (audit 2026-09-13, finding 9): the clerk-local binding generation,
    -- advanced by one exactly when an acknowledgement changes the effective
    -- (profile, revision, account) tuple. Stage, refused Apply, ordinary
    -- restart and an unchanged-tuple acknowledgement leave it alone.
    effective_binding_generation INTEGER NOT NULL DEFAULT 0
        CHECK (effective_binding_generation >= 0),
    effective_profile_id        TEXT,
    effective_revision          INTEGER,
    effective_account_id        TEXT,
    effective_acknowledged_at_ms INTEGER,
    last_apply_outcome          TEXT CHECK (last_apply_outcome IN ('applied', 'refused')),
    last_apply_refusal_reason   TEXT,
    CHECK ((staged_profile_id IS NULL) = (staged_revision IS NULL)),
    CHECK ((effective_profile_id IS NULL) = (effective_revision IS NULL)),
    FOREIGN KEY (staged_profile_id, staged_revision)
        REFERENCES profile_revisions(profile_id, revision),
    FOREIGN KEY (effective_profile_id, effective_revision)
        REFERENCES profile_revisions(profile_id, revision)
);
INSERT INTO "installation_selection" VALUES(1,'profile_fa686d23bb2d1208',1,0,NULL,NULL,1,0,NULL,NULL,NULL,NULL,NULL,NULL);
CREATE TABLE local_owner (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    -- UNIQUE so the owner references below resolve: SQLite requires a foreign
    -- key's parent column to be a primary key or carry a unique index.
    owner_id            TEXT NOT NULL UNIQUE,
    display_label       TEXT NOT NULL CHECK (length(display_label) > 0),
    created_at_ms       INTEGER NOT NULL,
    updated_at_ms       INTEGER NOT NULL
);
INSERT INTO "local_owner" VALUES(1,'owner_017507bde12844c0','test-operator',1757000000000,1757000000000);
CREATE TABLE profile_revisions (
    profile_id              TEXT NOT NULL REFERENCES broker_profiles(profile_id),
    revision                INTEGER NOT NULL CHECK (revision >= 1),
    schema_version          INTEGER NOT NULL,
    credential_slot         TEXT NOT NULL,
    endpoint_mode           TEXT NOT NULL CHECK (endpoint_mode IN ('paper', 'live')),
    account_pin             TEXT,
    account_pinned_at_ms    INTEGER,
    -- The six live-envelope values. REAL for the floats and INTEGER for the
    -- counts, so store -> load -> sha returns today's sha (ADR 0060 D6).
    -- NUMERIC is banned on this path; a Decimal raises rather than hashing.
    live_loss_fraction      REAL,
    live_loss_usd           REAL,
    live_shadow_sessions    INTEGER,
    live_arming_max_sessions INTEGER,
    live_xh_entry_bps       REAL,
    live_xh_exit_bps        REAL,
    content_sha256          TEXT NOT NULL,
    complete                INTEGER NOT NULL CHECK (complete IN (0, 1)),
    author_owner_id         TEXT NOT NULL REFERENCES local_owner(owner_id),
    created_at_ms           INTEGER NOT NULL,
    PRIMARY KEY (profile_id, revision),
    -- A live revision carries the whole envelope or none of it; a paper
    -- revision may carry none. Six-way all-or-nothing, in the schema.
    CHECK (
        (live_loss_fraction IS NULL AND live_loss_usd IS NULL
         AND live_shadow_sessions IS NULL AND live_arming_max_sessions IS NULL
         AND live_xh_entry_bps IS NULL AND live_xh_exit_bps IS NULL)
        OR
        (live_loss_fraction IS NOT NULL AND live_loss_usd IS NOT NULL
         AND live_shadow_sessions IS NOT NULL AND live_arming_max_sessions IS NOT NULL
         AND live_xh_entry_bps IS NOT NULL AND live_xh_exit_bps IS NOT NULL)
    ),
    CHECK ((account_pin IS NULL) = (account_pinned_at_ms IS NULL))
);
INSERT INTO "profile_revisions" VALUES('profile_fa686d23bb2d1208',1,1,'alpaca_paper_primary','paper','PA000PAPER',1757000003000,NULL,NULL,NULL,NULL,NULL,NULL,'7c62531f56ca43e13f94f3b66434ad30c1f7ac7d4e055ae045f581c58b4baa53',1,'owner_017507bde12844c0',1757000000000);
INSERT INTO "profile_revisions" VALUES('profile_00212df693131ff7',1,1,'alpaca_paper_secondary','paper',NULL,NULL,0.05,5000.0,3,20,11.0,17.5,'4e0309dd055b688f0aa197e45c3783ab3fd497eee30d562b4674674a22dbec0b',1,'owner_017507bde12844c0',1757000001000);
INSERT INTO "profile_revisions" VALUES('profile_ed16a6b5efa77d0d',1,1,'alpaca_live_primary','live',NULL,NULL,0.05,5000.0,3,20,11.0,17.5,'6326c6f02675c2b119bce9b0c2203ba017050aa2f168145eb6eef9fa12b945af',1,'owner_017507bde12844c0',1757000002000);
CREATE UNIQUE INDEX ux_broker_profiles_live_name
    ON broker_profiles(owner_id, display_name) WHERE archived = 0;
CREATE INDEX ix_profile_revisions_profile ON profile_revisions(profile_id, revision DESC);
CREATE UNIQUE INDEX ux_configuration_events_sequence ON configuration_events(sequence);
CREATE TRIGGER trg_profile_revisions_content_immutable
BEFORE UPDATE ON profile_revisions
FOR EACH ROW WHEN
    OLD.profile_id IS NOT NEW.profile_id
    OR OLD.revision IS NOT NEW.revision
    OR OLD.schema_version IS NOT NEW.schema_version
    OR OLD.credential_slot IS NOT NEW.credential_slot
    OR OLD.endpoint_mode IS NOT NEW.endpoint_mode
    OR OLD.live_loss_fraction IS NOT NEW.live_loss_fraction
    OR OLD.live_loss_usd IS NOT NEW.live_loss_usd
    OR OLD.live_shadow_sessions IS NOT NEW.live_shadow_sessions
    OR OLD.live_arming_max_sessions IS NOT NEW.live_arming_max_sessions
    OR OLD.live_xh_entry_bps IS NOT NEW.live_xh_entry_bps
    OR OLD.live_xh_exit_bps IS NOT NEW.live_xh_exit_bps
    OR OLD.content_sha256 IS NOT NEW.content_sha256
    OR OLD.complete IS NOT NEW.complete
    OR OLD.author_owner_id IS NOT NEW.author_owner_id
    OR OLD.created_at_ms IS NOT NEW.created_at_ms
    OR OLD.account_pin IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'a profile revision is immutable apart from binding its account pin once');
END;
CREATE TRIGGER trg_profile_revisions_no_delete
BEFORE DELETE ON profile_revisions
BEGIN
    SELECT RAISE(ABORT, 'a profile revision is never deleted; archive the profile instead');
END;
CREATE TRIGGER trg_configuration_events_no_update
BEFORE UPDATE ON configuration_events
BEGIN
    SELECT RAISE(ABORT, 'the configuration event log is append-only');
END;
CREATE TRIGGER trg_configuration_events_no_delete
BEFORE DELETE ON configuration_events
BEGIN
    SELECT RAISE(ABORT, 'the configuration event log is append-only');
END;
CREATE TRIGGER trg_installation_selection_generation_monotonic
BEFORE UPDATE ON installation_selection
FOR EACH ROW WHEN NEW.selection_generation < OLD.selection_generation
BEGIN
    SELECT RAISE(ABORT, 'the selection generation never moves backwards');
END;
COMMIT;
