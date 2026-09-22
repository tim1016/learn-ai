"""The fleet registry schema, its PRAGMAs and its versioned migrations.

A dedicated SQLite database on the coordinator's own control volume (PRD
FR-030), following the repository's established conventions — WAL,
``PRAGMA foreign_keys = ON``, a guarded singleton metadata row carrying the
schema version, additive-only registered migrations applied inside one
transaction, and uniqueness expressed in the schema rather than in service
code. The fresh schema for the current version lives here; the versioned
upgrade chain and the retained v1 shape live beside it in
``schema_migrations.py`` (re-exported below for the callers and tests that
read them off this module).

The registry is deliberately custody-free (PRD FR-023 / ADR 0062 Decision 1):
no table here names an order, fill, position, activation, arming or envelope
fact, and ``tests/broker/fleet/test_registry_contains_no_custody.py`` asserts
the shipped DDL never grows one. All timestamps are ``INTEGER`` ms UTC; no
textual datetime storage exists in this schema.

Schema v2 (audit 2026-09-13, finding 1/4/7) separates *observed* from
*confirmed* facts: the assignment row now carries the coordinator's confirmed
binding observation (generation, tuple, instance, epoch) and is the only
routing fence; sessions carry heartbeat observations only. Volume roots are
qualified by a deployment namespace so equal path strings in two containers
are not mistaken for one physical volume, and approved endpoints are
deployment-owned rows an agent can cite but never change. Routing receipts
carry the pinned attempt context and a four-state outcome vocabulary in which
a delivered outcome is terminal.

Schema v3 (#2073b) puts DDL behind the one structural-isolation invariant that
had none: a partial UNIQUE index on ``(deployment_namespace, volume_root)`` and
a BEFORE INSERT trigger doing exact prefix comparison, so "one clerk, one
physical volume" survives a race the service's pre-check cannot see.

Schema v4 (#2133 P2-a) adds the two indexes the routing-receipt audit read
surface needs and never had: ``routing_receipts`` had no secondary index
serving either its global or clerk-scoped newest-first read, so every audit
request scanned and sorted the whole append-only table while holding the
store's shared connection lock through ``fetchall()``. Both new indexes are
purely additive (``CREATE INDEX``, no table rewrite), so the upgrade is
non-destructive and safe to run against a live registry.

Schema v5 (ADR 0063, #2111) carries the drain ceremony's full inventory:
``clerks`` gains ``draining_since_ms`` and ``drain_deadline_at_ms`` (the
durable start instant and the absolute deadline the ceremony computes from
the deployment duration and the trading calendar) plus the retirement
attribution columns ``lane_confirmation``, ``retire_operator`` and
``retire_change_ref``; ``account_assignment_history`` gains the release and
reassignment attestation columns that replace ``RELEASE_PROOF_TOKEN``;
``routing_receipts`` gains the partial index serving the command-quiet
predicate; ``account_assignment_history`` gains the ``clerk_id`` index the
never-served predicate and the lifecycle trigger's backstop need; the
lifecycle trigger gains a fourth arm that aborts ``provisioned -> retired``
for a clerk that has served; and a new trigger pins drain and retirement
facts as write-once, so a repeated drain can never extend its own deadline
(§7.3). The v5 upgrade is additive except for that trigger arm — a registry
upgraded to v5 refuses a direct retirement it accepted at v4, which is the
point of Decision 6. Existing history rows keep NULL attestation columns
forever (the immutability trigger forbids the backfill UPDATE), so a reader
treats NULL as "recorded before v5", never as "absent confirmation".

Schema v6 (ADR 0063 Decision 2's 2026-09-19 amendment, #2154) adds
``clerk_lane_confirmations``, the durable evidence behind the retirement
gate: one row per answer a lane gives about its own quiescence, fenced by
the session that prepared it. ``clerks.lane_confirmation`` already recorded
the retirement *outcome*; it never held the evidence, so the gate had
nothing to read. The table is append-only like its siblings and keyed on an
append sequence rather than on the lane-supplied observation instant — the
gate must read the last answer *recorded*, which is the registry's own fact,
not the last one a lane host's clock claims to have observed. The upgrade is
purely additive (one CREATE TABLE, one index, two triggers), so it is
non-destructive against a live registry.

Schema v7 (ADR 0063 §4.1's 2026-09-21 amendment, #2154) makes "one live
assignment per clerk" structural: ``ix_account_assignments_owner`` becomes a
UNIQUE partial index over the non-released rows. That is what lets a
lane-quiet confirmation, which carries no account identity, authorize moving
one *named* account — a draining clerk reserves nothing new, so its one live
assignment when the drain began is the only account its confirmation can be
about. The upgrade replaces the index in place; a registry that already
holds two live assignments for one clerk fails the upgrade and rolls back
rather than being repaired, because which of the two is real is not the
schema's to decide.
"""

from __future__ import annotations

import sqlite3

from app.broker.fleet.schema_migrations import SCHEMA_DDL_V1, SCHEMA_MIGRATIONS
from app.utils.session_anchors import MAX_TIMESTAMP_MS

SCHEMA_VERSION = 7

#: Every ``*_ms`` column carries this bound in the schema, so a corrupt or
#: hostile write cannot persist a negative or out-of-range instant as fleet
#: evidence (ADR 0022's ``int64 ms UTC`` domain).
PRAGMA_STATEMENTS: tuple[str, ...] = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = FULL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
)

# The DDL spells the bound symbolically and the constant is substituted once,
# below, so the checked number lives in exactly one place.
_SCHEMA_DDL_TEMPLATE = """\
-- ============================================================
-- fleet_meta — guarded singleton carrying the schema version
-- ============================================================
CREATE TABLE fleet_meta (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version      INTEGER NOT NULL,
    registry_id         TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0 AND created_at_ms <= MAX_TIMESTAMP_MS),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0 AND updated_at_ms <= MAX_TIMESTAMP_MS)
);

-- ============================================================
-- clerks — durable lanes; identity columns are immutable
-- ============================================================
CREATE TABLE clerks (
    clerk_id                TEXT PRIMARY KEY,
    broker                  TEXT NOT NULL CHECK (length(broker) > 0),
    worker_key              TEXT NOT NULL,
    display_label           TEXT NOT NULL CHECK (length(display_label) > 0),
    volume_id               TEXT NOT NULL,
    -- The canonical mounted root recorded at provisioning, used only to
    -- refuse two clerks sharing writable subtrees of one physical volume
    -- (FR-020/021). Deployment detail: never projected over the public API.
    volume_root             TEXT NOT NULL CHECK (length(volume_root) > 0),
    -- The deployment namespace qualifies every root comparison: two
    -- containers may legitimately mount distinct volumes at the same path,
    -- and equal strings across namespaces prove nothing (audit 2026-09-13,
    -- finding 4). Immutable like the rest of the clerk's identity.
    deployment_namespace    TEXT NOT NULL CHECK (length(deployment_namespace) > 0),
    volume_attestation_kind TEXT NOT NULL CHECK (length(volume_attestation_kind) > 0),
    volume_attestation_id   TEXT NOT NULL CHECK (length(volume_attestation_id) > 0),
    lifecycle_state         TEXT NOT NULL CHECK (lifecycle_state IN ('provisioned', 'draining', 'retired')),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0 AND created_at_ms <= MAX_TIMESTAMP_MS),
    retired_at_ms INTEGER CHECK (retired_at_ms IS NULL OR (retired_at_ms >= 0 AND retired_at_ms <= MAX_TIMESTAMP_MS)),
    -- ADR 0063 Decision 1/5: the drain start instant and the absolute
    -- deadline computed at drain time. One-directional on purpose: draining
    -- requires both set, and a clerk that drained then retired keeps both —
    -- the pair is not cleared on retirement the way retired_at_ms is set.
    draining_since_ms INTEGER CHECK (draining_since_ms IS NULL OR (draining_since_ms >= 0 AND draining_since_ms <= MAX_TIMESTAMP_MS)),
    drain_deadline_at_ms INTEGER CHECK (drain_deadline_at_ms IS NULL OR (drain_deadline_at_ms >= 0 AND drain_deadline_at_ms <= MAX_TIMESTAMP_MS)),
    -- ADR 0063 Decision 5: force-retire's durable attribution. NULL until
    -- retired; 'absent' when force-retired past an unanswered lane-quiet
    -- gate; 'present' when the normal path retires on a lane confirmation.
    lane_confirmation TEXT CHECK (lane_confirmation IS NULL OR lane_confirmation IN ('absent', 'present')),
    retire_operator TEXT CHECK (retire_operator IS NULL OR (length(retire_operator) > 0 AND length(retire_operator) <= 128)),
    retire_change_ref TEXT CHECK (retire_change_ref IS NULL OR (length(retire_change_ref) > 0 AND length(retire_change_ref) <= 512)),
    CHECK ((retired_at_ms IS NULL) = (lifecycle_state <> 'retired')),
    CHECK (lifecycle_state <> 'draining' OR (draining_since_ms IS NOT NULL AND drain_deadline_at_ms IS NOT NULL))
);

-- Active clerks own unique worker keys, volumes and attestations (FR-032).
-- Partial indexes over the non-retired rows let a retired clerk's identifiers
-- rest in history without ever being recycled onto a new active clerk.
CREATE UNIQUE INDEX ux_clerks_worker_key
    ON clerks(worker_key) WHERE lifecycle_state <> 'retired';
CREATE UNIQUE INDEX ux_clerks_volume_id
    ON clerks(volume_id) WHERE lifecycle_state <> 'retired';
CREATE UNIQUE INDEX ux_clerks_volume_attestation
    ON clerks(deployment_namespace, volume_attestation_kind, volume_attestation_id)
    WHERE lifecycle_state <> 'retired';

-- FR-020/021: the equal-root half of "one clerk, one physical volume". The
-- nesting half needs a comparison an index cannot express and lives in
-- ``trg_clerks_volume_root_not_nested`` below.
CREATE UNIQUE INDEX ux_clerks_volume_root
    ON clerks(deployment_namespace, volume_root) WHERE lifecycle_state <> 'retired';

CREATE INDEX ix_clerks_listing ON clerks(broker, created_at_ms, clerk_id);

-- ============================================================
-- clerk_sessions — the one current registration per clerk.
-- Observed facts only: nothing here can confirm an assignment.
-- ============================================================
CREATE TABLE clerk_sessions (
    clerk_id                    TEXT PRIMARY KEY REFERENCES clerks(clerk_id),
    broker                      TEXT NOT NULL,
    agent_instance_id           TEXT NOT NULL,
    routing_epoch               INTEGER NOT NULL CHECK (routing_epoch >= 1),
    started_at_ms INTEGER NOT NULL CHECK (started_at_ms >= 0 AND started_at_ms <= MAX_TIMESTAMP_MS),
    last_seen_at_ms INTEGER NOT NULL CHECK (last_seen_at_ms >= 0 AND last_seen_at_ms <= MAX_TIMESTAMP_MS),
    reported_binding_generation INTEGER,
    reported_account_id         TEXT,
    reported_state              TEXT,
    -- The deployment-approved endpoint reference this registration cites;
    -- the destination itself lives only in approved_endpoints and can never
    -- be changed by a registration (audit 2026-09-13, finding 4).
    endpoint_ref                TEXT,
    -- The serving agent's adapter build, for the mixed-version matrix.
    adapter_version             TEXT,
    -- The agent's bounded, typed summary observation, stored as strict JSON
    -- (validated at ingestion; never free-form agent-authored JSON).
    reported_summary_json       TEXT
);

-- Every registration is kept, so a clerk's epoch history is auditable and a
-- duplicated live session is structurally impossible: the current row above
-- is the only place an agent can present itself.
CREATE TABLE clerk_session_history (
    clerk_id                    TEXT NOT NULL,
    broker                      TEXT NOT NULL,
    agent_instance_id           TEXT NOT NULL,
    routing_epoch               INTEGER NOT NULL CHECK (routing_epoch >= 1),
    started_at_ms INTEGER NOT NULL CHECK (started_at_ms >= 0 AND started_at_ms <= MAX_TIMESTAMP_MS),
    superseded_at_ms INTEGER NOT NULL CHECK (superseded_at_ms >= 0 AND superseded_at_ms <= MAX_TIMESTAMP_MS),
    PRIMARY KEY (clerk_id, routing_epoch)
);

-- ============================================================
-- approved_endpoints — deployment-owned internal destinations.
-- Written only by the host ceremony; a registration may cite a reference,
-- never change what it points at.
-- ============================================================
CREATE TABLE approved_endpoints (
    endpoint_ref    TEXT PRIMARY KEY CHECK (length(endpoint_ref) > 0),
    clerk_id        TEXT NOT NULL REFERENCES clerks(clerk_id),
    base_url        TEXT NOT NULL CHECK (length(base_url) > 0),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0 AND created_at_ms <= MAX_TIMESTAMP_MS),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0 AND updated_at_ms <= MAX_TIMESTAMP_MS)
);

CREATE UNIQUE INDEX ux_approved_endpoints_clerk ON approved_endpoints(clerk_id);

CREATE TRIGGER trg_approved_endpoints_no_delete
BEFORE DELETE ON approved_endpoints
BEGIN
    SELECT RAISE(ABORT, 'an approved endpoint is superseded by re-approval, never deleted');
END;

CREATE TRIGGER trg_approved_endpoints_identity_immutable
BEFORE UPDATE ON approved_endpoints
FOR EACH ROW WHEN
    OLD.endpoint_ref IS NOT NEW.endpoint_ref
    OR OLD.clerk_id IS NOT NEW.clerk_id
    OR OLD.created_at_ms IS NOT NEW.created_at_ms
BEGIN
    SELECT RAISE(ABORT, 'an approved endpoint belongs to its clerk and reference; re-approve the same reference with a new destination');
END;

-- ============================================================
-- account_assignments — the broker-qualified fence (PRD §9.6).
-- The confirmed_* columns are the coordinator's confirmed binding
-- observation: the exact facts a confirming worker presented, fenced by the
-- instance and epoch that presented them. They are a routing fence, not an
-- authority: the clerk-local selection transaction remains the single
-- authority for the effective tuple.
-- ============================================================
CREATE TABLE account_assignments (
    broker                          TEXT NOT NULL,
    canonical_external_account_id   TEXT NOT NULL CHECK (length(canonical_external_account_id) > 0),
    clerk_id                        TEXT NOT NULL REFERENCES clerks(clerk_id),
    assignment_generation           INTEGER NOT NULL CHECK (assignment_generation >= 1),
    state                           TEXT NOT NULL CHECK (state IN ('reserved', 'effective', 'released')),
    effective_profile_id            TEXT,
    effective_revision              INTEGER,
    confirmed_binding_generation    INTEGER,
    confirmed_profile_id            TEXT,
    confirmed_revision              INTEGER,
    confirmed_at_ms INTEGER CHECK (confirmed_at_ms IS NULL OR (confirmed_at_ms >= 0 AND confirmed_at_ms <= MAX_TIMESTAMP_MS)),
    confirmed_agent_instance_id     TEXT,
    confirmed_routing_epoch         INTEGER CHECK (confirmed_routing_epoch IS NULL OR confirmed_routing_epoch >= 1),
    recorded_at_ms INTEGER NOT NULL CHECK (recorded_at_ms >= 0 AND recorded_at_ms <= MAX_TIMESTAMP_MS),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0 AND updated_at_ms <= MAX_TIMESTAMP_MS),
    PRIMARY KEY (broker, canonical_external_account_id),
    CHECK ((effective_profile_id IS NULL) = (effective_revision IS NULL))
);

-- One live assignment per clerk (v7, #2154): a lane-quiet confirmation
-- names no account, so the account it covers must be unambiguous.
CREATE UNIQUE INDEX ix_account_assignments_owner
    ON account_assignments(clerk_id) WHERE state <> 'released';

-- ============================================================
-- routing_receipts — correlation only, never execution evidence.
-- The pinned_* context is written before dispatch and is immutable; the
-- outcome vocabulary separates not-dispatched, provider refusal, delivery
-- with a provider receipt, and unknown outcome. A delivered outcome is
-- terminal (audit 2026-09-13, finding 7).
-- ============================================================
CREATE TABLE routing_receipts (
    correlation_id      TEXT PRIMARY KEY,
    broker              TEXT NOT NULL,
    clerk_id            TEXT NOT NULL,
    operation_kind      TEXT NOT NULL,
    nonsecret_target_ref TEXT NOT NULL,
    idempotency_key     TEXT NOT NULL,
    state               TEXT NOT NULL CHECK (state IN ('not_dispatched', 'provider_refused', 'delivered', 'outcome_unknown')),
    upstream_receipt_ref TEXT,
    pinned_routing_epoch        INTEGER CHECK (pinned_routing_epoch IS NULL OR pinned_routing_epoch >= 1),
    pinned_binding_generation   INTEGER,
    pinned_agent_instance_id    TEXT,
    dispatched_at_ms INTEGER CHECK (dispatched_at_ms IS NULL OR (dispatched_at_ms >= 0 AND dispatched_at_ms <= MAX_TIMESTAMP_MS)),
    created_at_ms INTEGER NOT NULL CHECK (created_at_ms >= 0 AND created_at_ms <= MAX_TIMESTAMP_MS),
    updated_at_ms INTEGER NOT NULL CHECK (updated_at_ms >= 0 AND updated_at_ms <= MAX_TIMESTAMP_MS)
);

-- One durable idempotency identity per lane and key (PRD FR-078): a retry
-- updates the same receipt instead of appending a sibling.
CREATE UNIQUE INDEX ux_routing_receipts_idempotency
    ON routing_receipts(broker, clerk_id, idempotency_key);

-- The audit read surface's two newest-first access paths (#2133 P2-a): global
-- and clerk-scoped. Both match the audit query's ORDER BY exactly, tiebreak
-- column included, so SQLite can walk the index backwards for the query's
-- descending order instead of scanning the whole append-only table and
-- sorting it under the store's shared connection lock.
CREATE INDEX ix_routing_receipts_created_at
    ON routing_receipts(created_at_ms, correlation_id);
CREATE INDEX ix_routing_receipts_clerk_created_at
    ON routing_receipts(clerk_id, created_at_ms, correlation_id);

-- ADR 0063 Decision 3: the command-quiet predicate — an attempt the
-- coordinator dispatched and never heard the outcome of — served by index
-- rather than a scan of the append-only receipts table.
CREATE INDEX ix_routing_receipts_unsettled
    ON routing_receipts(clerk_id)
    WHERE state = 'not_dispatched' AND dispatched_at_ms IS NOT NULL;

-- ============================================================
-- force_retire_correlations — the forced-unknown obligations a
-- force-retirement created, written in the same transaction as the
-- retirement itself (ADR 0063 Decision 5's open gap: they outlive the
-- retired clerk, so they need a home no successor has to describe).
-- A registry table, not a sidecar file, deliberately: atomic with the
-- transition and included in every registry backup.
-- ============================================================
CREATE TABLE force_retire_correlations (
    correlation_id  TEXT PRIMARY KEY,
    broker          TEXT NOT NULL CHECK (length(broker) > 0),
    clerk_id        TEXT NOT NULL CHECK (length(clerk_id) > 0),
    forced_at_ms INTEGER NOT NULL CHECK (forced_at_ms >= 0 AND forced_at_ms <= MAX_TIMESTAMP_MS),
    operator TEXT NOT NULL CHECK (length(operator) > 0 AND length(operator) <= 128),
    change_ref TEXT NOT NULL CHECK (length(change_ref) > 0 AND length(change_ref) <= 512)
);

CREATE TRIGGER trg_force_retire_correlations_immutable
BEFORE UPDATE ON force_retire_correlations
BEGIN
    SELECT RAISE(ABORT, 'forced-unknown obligations are append-only');
END;

CREATE TRIGGER trg_force_retire_correlations_no_delete
BEFORE DELETE ON force_retire_correlations
BEGIN
    SELECT RAISE(ABORT, 'forced-unknown obligations are append-only');
END;

-- ============================================================
-- clerk_lane_confirmations — ADR 0063 Decision 2 (2026-09-19
-- amendment, #2154). One lane's answer about its own quiescence at
-- one observed instant, fenced by the session that prepared it. The
-- gate reads the NEWEST row for the clerk's CURRENT session: a
-- restart destroys the running-process knowledge conditions 2, 3 and
-- 5 assert, so the row survives a session change while its authority
-- does not.
--
-- Keyed on an append sequence, not on anything the lane supplies.
-- ``observed_at_ms`` arrives from the lane host, so keying or
-- ordering on it would let a host whose clock stepped back decide
-- which of its own answers the gate reads — and retire itself on a
-- quiet claim it had already withdrawn. Newest here means last
-- appended, which is the registry's own fact. The instant is kept
-- because freshness is measured against it (Decision 2's amendment),
-- never to order or identify a row.
--
-- Append-only, like every other evidence table here: what a lane
-- claimed, and when, stays auditable after the clerk is terminal.
-- Custody-free by construction — four booleans and two instants,
-- never a quantity, a symbol or an identifier. The conditions are
-- nullity predicates about the lane's own state: "the account is
-- flat" records no position, and "a working order has not ended"
-- records no order.
-- ============================================================
CREATE TABLE clerk_lane_confirmations (
    confirmation_seq  INTEGER PRIMARY KEY,
    clerk_id          TEXT NOT NULL CHECK (length(clerk_id) > 0),
    agent_instance_id TEXT NOT NULL CHECK (length(agent_instance_id) > 0),
    routing_epoch  INTEGER NOT NULL CHECK (routing_epoch >= 1),
    observed_at_ms INTEGER NOT NULL CHECK (observed_at_ms >= 0 AND observed_at_ms <= MAX_TIMESTAMP_MS),
    recorded_at_ms INTEGER NOT NULL CHECK (recorded_at_ms >= 0 AND recorded_at_ms <= MAX_TIMESTAMP_MS),
    runner_idle       INTEGER NOT NULL CHECK (runner_idle IN (0, 1)),
    broker_work_ended INTEGER NOT NULL CHECK (broker_work_ended IN (0, 1)),
    account_flat      INTEGER NOT NULL CHECK (account_flat IN (0, 1)),
    intents_resolved  INTEGER NOT NULL CHECK (intents_resolved IN (0, 1))
);

CREATE INDEX ix_clerk_lane_confirmations_latest
    ON clerk_lane_confirmations(clerk_id, agent_instance_id, routing_epoch, confirmation_seq DESC);

CREATE TRIGGER trg_clerk_lane_confirmations_immutable
BEFORE UPDATE ON clerk_lane_confirmations
BEGIN
    SELECT RAISE(ABORT, 'lane-quiet confirmations are append-only');
END;

CREATE TRIGGER trg_clerk_lane_confirmations_no_delete
BEFORE DELETE ON clerk_lane_confirmations
BEGIN
    SELECT RAISE(ABORT, 'lane-quiet confirmations are append-only');
END;

-- ============================================================
-- The invariants that are the schema's job, not a caller's
-- ============================================================

-- A clerk's identity never changes: broker, key, volume and attestation are
-- written once at provisioning, and rows are never deleted — retirement is a
-- lifecycle transition, and IDs are never recycled (PRD FR-013).
CREATE TRIGGER trg_clerks_identity_immutable
BEFORE UPDATE ON clerks
FOR EACH ROW WHEN
    OLD.clerk_id IS NOT NEW.clerk_id
    OR OLD.broker IS NOT NEW.broker
    OR OLD.worker_key IS NOT NEW.worker_key
    OR OLD.volume_id IS NOT NEW.volume_id
    OR OLD.volume_root IS NOT NEW.volume_root
    OR OLD.deployment_namespace IS NOT NEW.deployment_namespace
    OR OLD.volume_attestation_kind IS NOT NEW.volume_attestation_kind
    OR OLD.volume_attestation_id IS NOT NEW.volume_attestation_id
    OR OLD.created_at_ms IS NOT NEW.created_at_ms
BEGIN
    SELECT RAISE(ABORT, 'a clerk identity is written once at provisioning and never changes');
END;

CREATE TRIGGER trg_clerks_no_delete
BEFORE DELETE ON clerks
BEGIN
    SELECT RAISE(ABORT, 'a clerk is retired, never deleted; IDs are not recycled');
END;

-- Lifecycle moves forward only: provisioned -> draining -> retired, with a
-- direct provisioned -> retired allowed only for a clerk that never served
-- (ADR 0063 Decision 6). The fourth arm mirrors the service's three-clause
-- never-served predicate exactly — no row in the append-only assignment
-- history, none in the append-only session history, and no live
-- clerk_sessions row — because that table is a per-clerk upsert that is
-- never deleted: a clerk that registered exactly once holds a live row and
-- zero history rows, so the two history tables alone would misclassify it
-- as never-served.
CREATE TRIGGER trg_clerks_lifecycle_forward
BEFORE UPDATE ON clerks
FOR EACH ROW WHEN
    (OLD.lifecycle_state = 'provisioned' AND NEW.lifecycle_state = 'provisioned' AND OLD.retired_at_ms IS NOT NEW.retired_at_ms)
    OR (OLD.lifecycle_state = 'draining' AND NEW.lifecycle_state NOT IN ('draining', 'retired'))
    OR (OLD.lifecycle_state = 'retired' AND NEW.lifecycle_state <> 'retired')
    OR (OLD.lifecycle_state = 'provisioned' AND NEW.lifecycle_state = 'retired' AND (
        EXISTS (SELECT 1 FROM account_assignment_history WHERE clerk_id = OLD.clerk_id)
        OR EXISTS (SELECT 1 FROM clerk_session_history WHERE clerk_id = OLD.clerk_id)
        OR EXISTS (SELECT 1 FROM clerk_sessions WHERE clerk_id = OLD.clerk_id)
    ))
BEGIN
    SELECT RAISE(ABORT, 'a clerk lifecycle moves forward only, and a clerk that has served retires through draining');
END;

-- ADR 0063 §7.3: drain facts are written once at the drain and never move —
-- a repeated drain must not extend its own deadline, or a retry loop makes
-- the bound decorative. Retirement attribution moves only on the transition
-- into 'retired' and never afterwards.
CREATE TRIGGER trg_clerks_closeout_facts_write_once
BEFORE UPDATE ON clerks
FOR EACH ROW WHEN
    (OLD.lifecycle_state <> 'provisioned' AND (
        OLD.draining_since_ms IS NOT NEW.draining_since_ms
        OR OLD.drain_deadline_at_ms IS NOT NEW.drain_deadline_at_ms
    ))
    OR (OLD.lifecycle_state = 'retired' AND (
        OLD.lane_confirmation IS NOT NEW.lane_confirmation
        OR OLD.retire_operator IS NOT NEW.retire_operator
        OR OLD.retire_change_ref IS NOT NEW.retire_change_ref
    ))
BEGIN
    SELECT RAISE(ABORT, 'a clerk''s drain and retirement facts are written once and never rewritten');
END;

-- FR-020/021: writable subtrees of one mounted volume never host two clerks.
-- The service's pre-check produces the typed refusal; this is the backstop a
-- race cannot step over. ``substr`` rather than ``LIKE``: a volume root
-- containing ``_`` or ``%`` must not over-match a wildcard pattern.
CREATE TRIGGER trg_clerks_volume_root_not_nested
BEFORE INSERT ON clerks
FOR EACH ROW WHEN NEW.lifecycle_state <> 'retired' AND EXISTS (
    SELECT 1 FROM clerks existing
    WHERE existing.lifecycle_state <> 'retired'
      AND existing.deployment_namespace = NEW.deployment_namespace
      AND (substr(NEW.volume_root, 1, length(existing.volume_root) + 1)
               = existing.volume_root || '/'
           OR substr(existing.volume_root, 1, length(NEW.volume_root) + 1)
               = NEW.volume_root || '/')
)
BEGIN
    SELECT RAISE(ABORT, 'one clerk, one physical volume: a volume root never nests inside another active clerk''s root');
END;

-- The current session's epoch never decreases; history rows never change.
CREATE TRIGGER trg_clerk_sessions_epoch_monotonic
BEFORE UPDATE ON clerk_sessions
FOR EACH ROW WHEN NEW.routing_epoch < OLD.routing_epoch
BEGIN
    SELECT RAISE(ABORT, 'a routing epoch never moves backwards');
END;

CREATE TRIGGER trg_clerk_sessions_no_delete
BEFORE DELETE ON clerk_sessions
BEGIN
    SELECT RAISE(ABORT, 'a superseded session is archived, not deleted');
END;

CREATE TRIGGER trg_clerk_session_history_immutable
BEFORE UPDATE ON clerk_session_history
BEGIN
    SELECT RAISE(ABORT, 'session history is append-only');
END;

CREATE TRIGGER trg_clerk_session_history_no_delete
BEFORE DELETE ON clerk_session_history
BEGIN
    SELECT RAISE(ABORT, 'session history is append-only');
END;

-- Assignment generations are monotonic and rows are never deleted: released
-- stays as terminal history, which is what makes "never expire into takeover"
-- auditable (PRD FR-053/054).
CREATE TRIGGER trg_account_assignments_generation_monotonic
BEFORE UPDATE ON account_assignments
FOR EACH ROW WHEN NEW.assignment_generation < OLD.assignment_generation
BEGIN
    SELECT RAISE(ABORT, 'an assignment generation never moves backwards');
END;

CREATE TRIGGER trg_account_assignments_no_delete
BEFORE DELETE ON account_assignments
BEGIN
    SELECT RAISE(ABORT, 'an assignment is released, never deleted');
END;

-- A receipt is inserted once with its pinned attempt context; only the
-- outcome state, upstream reference, dispatch time and updated timestamp may
-- move.
CREATE TRIGGER trg_routing_receipts_outcome_only
BEFORE UPDATE ON routing_receipts
FOR EACH ROW WHEN
    OLD.correlation_id IS NOT NEW.correlation_id
    OR OLD.broker IS NOT NEW.broker
    OR OLD.clerk_id IS NOT NEW.clerk_id
    OR OLD.operation_kind IS NOT NEW.operation_kind
    OR OLD.nonsecret_target_ref IS NOT NEW.nonsecret_target_ref
    OR OLD.idempotency_key IS NOT NEW.idempotency_key
    OR OLD.pinned_routing_epoch IS NOT NEW.pinned_routing_epoch
    OR OLD.pinned_binding_generation IS NOT NEW.pinned_binding_generation
    OR OLD.pinned_agent_instance_id IS NOT NEW.pinned_agent_instance_id
    OR OLD.created_at_ms IS NOT NEW.created_at_ms
BEGIN
    SELECT RAISE(ABORT, 'a routing receipt identity and pinned context are immutable; only its outcome may move');
END;

CREATE TRIGGER trg_routing_receipts_no_delete
BEFORE DELETE ON routing_receipts
BEGIN
    SELECT RAISE(ABORT, 'a routing receipt is never deleted');
END;

-- A delivered outcome is terminal: a late failed retry must never erase a
-- known successful result (audit 2026-09-13, finding 7).
CREATE TRIGGER trg_routing_receipts_delivered_terminal
BEFORE UPDATE ON routing_receipts
FOR EACH ROW WHEN OLD.state = 'delivered' AND NEW.state <> 'delivered'
BEGIN
    SELECT RAISE(ABORT, 'a delivered routing outcome is never downgraded');
END;

-- Dispatch is one-way: once an attempt was dispatched it stays dispatched,
-- so "definitively not dispatched" remains provable.
CREATE TRIGGER trg_routing_receipts_dispatch_monotonic
BEFORE UPDATE ON routing_receipts
FOR EACH ROW WHEN OLD.dispatched_at_ms IS NOT NULL AND NEW.dispatched_at_ms IS NULL
BEGIN
    SELECT RAISE(ABORT, 'a dispatched routing attempt is never un-dispatched');
END;

-- ============================================================
-- account_assignment_history — append-only audit of every
-- assignment transition; the current row above is the pointer
-- ============================================================
CREATE TABLE account_assignment_history (
    broker                          TEXT NOT NULL,
    canonical_external_account_id   TEXT NOT NULL,
    clerk_id                        TEXT NOT NULL,
    assignment_generation           INTEGER NOT NULL CHECK (assignment_generation >= 1),
    state                           TEXT NOT NULL CHECK (state IN ('reserved', 'effective', 'released')),
    effective_profile_id            TEXT,
    effective_revision              INTEGER,
    recorded_at_ms                  INTEGER NOT NULL CHECK (recorded_at_ms >= 0 AND recorded_at_ms <= MAX_TIMESTAMP_MS),
    updated_at_ms                   INTEGER NOT NULL CHECK (updated_at_ms >= 0 AND updated_at_ms <= MAX_TIMESTAMP_MS),
    -- ADR 0063 Decision 4/4.1: the release and reassignment attestations
    -- that replace RELEASE_PROOF_TOKEN, written only at the INSERT of the
    -- ceremony's history row. Existing rows carry NULL forever (the
    -- immutability trigger below forbids the backfill UPDATE); a reader
    -- treats NULL as "recorded before v5", never as "absent confirmation".
    lane_confirmation TEXT CHECK (lane_confirmation IS NULL OR lane_confirmation IN ('absent', 'present')),
    attested_operator TEXT CHECK (attested_operator IS NULL OR (length(attested_operator) > 0 AND length(attested_operator) <= 128)),
    attested_change_ref TEXT CHECK (attested_change_ref IS NULL OR (length(attested_change_ref) > 0 AND length(attested_change_ref) <= 512)),
    attested_at_ms INTEGER CHECK (attested_at_ms IS NULL OR (attested_at_ms >= 0 AND attested_at_ms <= MAX_TIMESTAMP_MS)),
    PRIMARY KEY (broker, canonical_external_account_id, assignment_generation, state)
);

-- ADR 0063 Decision 6: the never-served predicate and the lifecycle
-- trigger's backstop both read this table by clerk; its primary key does
-- not lead with clerk_id, so this index is the supporting read path.
CREATE INDEX ix_account_assignment_history_clerk
    ON account_assignment_history(clerk_id);

CREATE TRIGGER trg_account_assignment_history_immutable
BEFORE UPDATE ON account_assignment_history
BEGIN
    SELECT RAISE(ABORT, 'assignment history is append-only');
END;

CREATE TRIGGER trg_account_assignment_history_no_delete
BEFORE DELETE ON account_assignment_history
BEGIN
    SELECT RAISE(ABORT, 'assignment history is append-only');
END;
"""

SCHEMA_DDL = _SCHEMA_DDL_TEMPLATE.replace("MAX_TIMESTAMP_MS", str(MAX_TIMESTAMP_MS))


def configure_connection(conn: sqlite3.Connection) -> None:
    """Apply the pinned PRAGMA set to a freshly-opened connection.

    ``PRAGMA journal_mode`` returns the mode SQLite actually retained, not the
    mode asked for: on a filesystem where WAL is unsupported it can silently
    fall back to a rollback journal. The returned mode is therefore inspected
    and anything other than ``wal`` refuses, so the concurrency-sensitive
    registry never opens in a weaker mode than its fences assume.
    """
    for statement in PRAGMA_STATEMENTS:
        cursor = conn.execute(statement)
        if statement == "PRAGMA journal_mode = WAL":
            retained = str(cursor.fetchone()[0]).lower()
            if retained != "wal":
                raise ValueError(
                    f"the fleet registry requires WAL journaling; SQLite retained "
                    f"{retained!r} instead — mount the control volume from a "
                    "container-local named volume"
                )


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create every table, index and trigger (fresh init only)."""
    conn.executescript(SCHEMA_DDL)


def is_upgradable_to_current(version: int) -> bool:
    """Whether a chained, registered migration path reaches ``SCHEMA_VERSION``."""
    seen = version
    while seen < SCHEMA_VERSION:
        if seen not in SCHEMA_MIGRATIONS:
            return False
        seen += 1
    return True


def migrate_schema(conn: sqlite3.Connection, *, from_version: int) -> None:
    """Apply every registered upgrade from ``from_version`` to ``SCHEMA_VERSION``.

    One transaction, so a partially-applied upgrade can never be observed.
    An unregistered version raises rather than guessing.
    """
    version = from_version
    if version == SCHEMA_VERSION:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        while version < SCHEMA_VERSION:
            statements = SCHEMA_MIGRATIONS.get(version)
            if statements is None:
                raise ValueError(
                    f"no registered migration from schema_version={version} to {version + 1}"
                )
            for statement in statements:
                conn.execute(statement)
            version += 1
        conn.execute(
            "UPDATE fleet_meta SET schema_version = ? WHERE id = 1",
            (SCHEMA_VERSION,),
        )
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


__all__ = [
    "PRAGMA_STATEMENTS",
    "SCHEMA_DDL",
    "SCHEMA_DDL_V1",
    "SCHEMA_MIGRATIONS",
    "SCHEMA_VERSION",
    "apply_schema",
    "configure_connection",
    "is_upgradable_to_current",
    "migrate_schema",
]  # SCHEMA_DDL_V1 and SCHEMA_MIGRATIONS are re-exported from schema_migrations
