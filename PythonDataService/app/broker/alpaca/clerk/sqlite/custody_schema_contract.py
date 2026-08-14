"""SQLite DDL fragments for the v9 custody-subject integrity boundary.

The main schema module owns the ordered full contract.  These fragments keep
the dense subject/resource invariants together so they can be reviewed without
mixing them into the historic schema and migration implementation.
"""

CUSTODY_SUBJECT_IDENTITY_DDL = """\
CREATE TRIGGER trg_custody_subject_identity_immutable
BEFORE UPDATE OF subject_id, kind, strategy_instance_id, operator_id ON custody_subjects
BEGIN
    SELECT RAISE(ABORT, 'custody_subjects identity is immutable');
END;
CREATE TRIGGER trg_custody_subject_delete_forbidden
BEFORE DELETE ON custody_subjects
BEGIN
    SELECT RAISE(ABORT, 'custody_subjects are append-only');
END;
"""


COMMAND_SUBJECT_COMPATIBILITY_DDL = """\
CREATE TRIGGER trg_commands_subject_compatible_insert
BEFORE INSERT ON commands
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM custody_subjects
        WHERE subject_id = NEW.subject_id AND (
            (kind = 'BOT' AND strategy_instance_id = NEW.strategy_instance_id
                AND NEW.kind IN ('strategy_decision', 'operator_lifecycle'))
            OR (kind = 'MANUAL_OPERATOR' AND NEW.kind = 'manual_order'
                AND NEW.strategy_instance_id IS NULL AND NEW.run_id IS NULL)
        )
    ) THEN RAISE(ABORT, 'commands subject must own its bot strategy or be a strategy-free manual order') END;
END;
CREATE TRIGGER trg_commands_subject_compatible_update
BEFORE UPDATE OF subject_id, kind, strategy_instance_id, run_id ON commands
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM custody_subjects
        WHERE subject_id = NEW.subject_id AND (
            (kind = 'BOT' AND strategy_instance_id = NEW.strategy_instance_id
                AND NEW.kind IN ('strategy_decision', 'operator_lifecycle'))
            OR (kind = 'MANUAL_OPERATOR' AND NEW.kind = 'manual_order'
                AND NEW.strategy_instance_id IS NULL AND NEW.run_id IS NULL)
        )
    ) THEN RAISE(ABORT, 'commands subject must own its bot strategy or be a strategy-free manual order') END;
END;
"""


_EFFECT_SUBJECT_COMPATIBILITY_INSERT_V9_DDL = """\
CREATE TRIGGER trg_effect_operations_subject_compatible_insert
BEFORE INSERT ON effect_operations
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM commands command
        JOIN custody_subjects subject ON subject.subject_id = NEW.subject_id
        WHERE command.command_id = NEW.command_id
            AND command.subject_id = NEW.subject_id
            AND command.strategy_instance_id IS NEW.strategy_instance_id
            AND command.run_id IS NEW.run_id
            AND (
                (subject.kind = 'BOT' AND subject.strategy_instance_id = NEW.strategy_instance_id
                    AND NEW.kind != 'MANUAL_ORDER')
                OR (subject.kind = 'MANUAL_OPERATOR' AND NEW.kind = 'MANUAL_ORDER'
                    AND NEW.strategy_instance_id IS NULL AND NEW.run_id IS NULL)
            )
    ) THEN RAISE(ABORT, 'effect operation must stay within its command custody subject') END;
END;
"""


_EFFECT_SUBJECT_COMPATIBILITY_UPDATE_V9_DDL = """\
CREATE TRIGGER trg_effect_operations_subject_compatible_update
BEFORE UPDATE OF subject_id, command_id, strategy_instance_id, run_id, kind ON effect_operations
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM commands command
        JOIN custody_subjects subject ON subject.subject_id = NEW.subject_id
        WHERE command.command_id = NEW.command_id
            AND command.subject_id = NEW.subject_id
            AND command.strategy_instance_id IS NEW.strategy_instance_id
            AND command.run_id IS NEW.run_id
            AND (
                (subject.kind = 'BOT' AND subject.strategy_instance_id = NEW.strategy_instance_id
                    AND NEW.kind != 'MANUAL_ORDER')
                OR (subject.kind = 'MANUAL_OPERATOR' AND NEW.kind = 'MANUAL_ORDER'
                    AND NEW.strategy_instance_id IS NULL AND NEW.run_id IS NULL)
            )
    ) THEN RAISE(ABORT, 'effect operation must stay within its command custody subject') END;
END;
"""

# Schema v9 only admitted manual-order effects.  v10 replaces these two
# triggers atomically before it introduces the durable cancellation resource.
EFFECT_SUBJECT_COMPATIBILITY_DDL = (
    _EFFECT_SUBJECT_COMPATIBILITY_INSERT_V9_DDL
    + _EFFECT_SUBJECT_COMPATIBILITY_UPDATE_V9_DDL
)
_EFFECT_SUBJECT_COMPATIBILITY_INSERT_V10_DDL = (
    _EFFECT_SUBJECT_COMPATIBILITY_INSERT_V9_DDL.replace(
        "NEW.kind = 'MANUAL_ORDER'",
        "NEW.kind IN ('MANUAL_ORDER', 'CANCEL')",
    )
)
_EFFECT_SUBJECT_COMPATIBILITY_UPDATE_V10_DDL = (
    _EFFECT_SUBJECT_COMPATIBILITY_UPDATE_V9_DDL.replace(
        "NEW.kind = 'MANUAL_ORDER'",
        "NEW.kind IN ('MANUAL_ORDER', 'CANCEL')",
    )
)
EFFECT_SUBJECT_COMPATIBILITY_V10_MIGRATION_STATEMENTS: tuple[str, ...] = (
    "DROP TRIGGER trg_effect_operations_subject_compatible_insert",
    "DROP TRIGGER trg_effect_operations_subject_compatible_update",
    _EFFECT_SUBJECT_COMPATIBILITY_INSERT_V10_DDL,
    _EFFECT_SUBJECT_COMPATIBILITY_UPDATE_V10_DDL,
)


POSITION_SUBJECT_COMPATIBILITY_DDL = """\
CREATE TRIGGER trg_positions_subject_compatible_insert
BEFORE INSERT ON positions
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM custody_subjects
        WHERE subject_id = NEW.subject_id AND (
            (kind = 'BOT' AND strategy_instance_id = NEW.strategy_instance_id)
            OR (kind = 'MANUAL_OPERATOR' AND NEW.strategy_instance_id IS NULL)
        )
    ) THEN RAISE(ABORT, 'position subject must own its bot strategy or be manual without strategy') END;
END;
CREATE TRIGGER trg_positions_subject_compatible_update
BEFORE UPDATE OF subject_id, strategy_instance_id ON positions
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM custody_subjects
        WHERE subject_id = NEW.subject_id AND (
            (kind = 'BOT' AND strategy_instance_id = NEW.strategy_instance_id)
            OR (kind = 'MANUAL_OPERATOR' AND NEW.strategy_instance_id IS NULL)
        )
    ) THEN RAISE(ABORT, 'position subject must own its bot strategy or be manual without strategy') END;
END;
"""


HOLD_SUBJECT_COMPATIBILITY_DDL = """\
CREATE TRIGGER trg_holds_subject_compatible_insert
BEFORE INSERT ON holds
WHEN NEW.scope = 'CUSTODY_SUBJECT'
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM custody_subjects
        WHERE subject_id = NEW.subject_id AND (
            (kind = 'BOT' AND strategy_instance_id = NEW.strategy_instance_id)
            OR (kind = 'MANUAL_OPERATOR' AND NEW.strategy_instance_id IS NULL)
        )
    ) THEN RAISE(ABORT, 'hold subject must own its bot strategy or be manual without strategy') END;
END;
CREATE TRIGGER trg_holds_subject_compatible_update
BEFORE UPDATE OF scope, subject_id, strategy_instance_id ON holds
WHEN NEW.scope = 'CUSTODY_SUBJECT'
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM custody_subjects
        WHERE subject_id = NEW.subject_id AND (
            (kind = 'BOT' AND strategy_instance_id = NEW.strategy_instance_id)
            OR (kind = 'MANUAL_OPERATOR' AND NEW.strategy_instance_id IS NULL)
        )
    ) THEN RAISE(ABORT, 'hold subject must own its bot strategy or be manual without strategy') END;
END;
"""


UNCERTAINTY_SUBJECT_COMPATIBILITY_DDL = """\
CREATE TRIGGER trg_uncertainties_subject_compatible_insert
BEFORE INSERT ON uncertainties
WHEN NEW.scope = 'CUSTODY_SUBJECT'
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM custody_subjects
        WHERE subject_id = NEW.subject_id AND (
            (kind = 'BOT' AND strategy_instance_id = NEW.strategy_instance_id)
            OR (kind = 'MANUAL_OPERATOR' AND NEW.strategy_instance_id IS NULL)
        )
    ) THEN RAISE(ABORT, 'uncertainty subject must own its bot strategy or be manual without strategy') END;
END;
CREATE TRIGGER trg_uncertainties_subject_compatible_update
BEFORE UPDATE OF scope, subject_id, strategy_instance_id ON uncertainties
WHEN NEW.scope = 'CUSTODY_SUBJECT'
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM custody_subjects
        WHERE subject_id = NEW.subject_id AND (
            (kind = 'BOT' AND strategy_instance_id = NEW.strategy_instance_id)
            OR (kind = 'MANUAL_OPERATOR' AND NEW.strategy_instance_id IS NULL)
        )
    ) THEN RAISE(ABORT, 'uncertainty subject must own its bot strategy or be manual without strategy') END;
END;
"""


MANUAL_TICKET_SUBJECT_COMPATIBILITY_DDL = """\
CREATE TRIGGER trg_manual_order_tickets_subject_compatible_insert
BEFORE INSERT ON manual_order_tickets
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM custody_subjects
        WHERE subject_id = NEW.subject_id
            AND kind = 'MANUAL_OPERATOR'
            AND operator_id = NEW.operator_id
    ) THEN RAISE(ABORT, 'manual ticket requires its canonical manual operator subject') END;
END;
CREATE TRIGGER trg_manual_order_tickets_subject_compatible_update
BEFORE UPDATE OF subject_id, operator_id ON manual_order_tickets
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM custody_subjects
        WHERE subject_id = NEW.subject_id
            AND kind = 'MANUAL_OPERATOR'
            AND operator_id = NEW.operator_id
    ) THEN RAISE(ABORT, 'manual ticket requires its canonical manual operator subject') END;
END;
CREATE TRIGGER trg_manual_order_ticket_identity_immutable
BEFORE UPDATE OF ticket_id, subject_id, operator_id, instruction_hash ON manual_order_tickets
BEGIN
    SELECT RAISE(ABORT, 'manual_order_tickets identity is immutable');
END;
CREATE TRIGGER trg_manual_order_ticket_delete_forbidden
BEFORE DELETE ON manual_order_tickets
BEGIN
    SELECT RAISE(ABORT, 'manual_order_tickets are append-only');
END;
"""


MANUAL_LEG_SUBJECT_COMPATIBILITY_DDL = """\
CREATE TRIGGER trg_manual_order_leg_identity_immutable
BEFORE UPDATE OF ticket_id, leg_id, subject_id, instruction_hash ON manual_order_legs
BEGIN
    SELECT RAISE(ABORT, 'manual_order_legs identity is immutable');
END;
CREATE TRIGGER trg_manual_order_leg_delete_forbidden
BEFORE DELETE ON manual_order_legs
BEGIN
    SELECT RAISE(ABORT, 'manual_order_legs are append-only');
END;
CREATE TRIGGER trg_manual_order_legs_subject_compatible_insert
BEFORE INSERT ON manual_order_legs
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM manual_order_tickets ticket
        JOIN custody_subjects subject ON subject.subject_id = ticket.subject_id
        WHERE ticket.ticket_id = NEW.ticket_id
            AND ticket.subject_id = NEW.subject_id
            AND subject.kind = 'MANUAL_OPERATOR'
    ) THEN RAISE(ABORT, 'manual leg must belong to its ticket manual subject') END;
    SELECT CASE WHEN (NEW.command_id IS NULL) != (NEW.effect_operation_id IS NULL)
        OR (NEW.command_id IS NULL) != (NEW.order_ref IS NULL)
        THEN RAISE(ABORT, 'manual leg resources must be all null or one complete manual chain') END;
    SELECT CASE WHEN NEW.command_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM commands command
        JOIN effect_operations effect ON effect.effect_operation_id = NEW.effect_operation_id
        JOIN orders ord ON ord.order_ref = NEW.order_ref
        WHERE command.command_id = NEW.command_id
            AND command.subject_id = NEW.subject_id
            AND command.kind = 'manual_order'
            AND command.strategy_instance_id IS NULL
            AND command.run_id IS NULL
            AND effect.command_id = command.command_id
            AND effect.subject_id = NEW.subject_id
            AND effect.kind = 'MANUAL_ORDER'
            AND effect.strategy_instance_id IS NULL
            AND effect.run_id IS NULL
            AND ord.effect_operation_id = effect.effect_operation_id
            AND ord.role = 'MANUAL'
    ) THEN RAISE(ABORT, 'manual leg resources must be one chain in the ticket subject') END;
END;
CREATE TRIGGER trg_manual_order_legs_subject_compatible_update
BEFORE UPDATE OF ticket_id, subject_id, command_id, effect_operation_id, order_ref ON manual_order_legs
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM manual_order_tickets ticket
        JOIN custody_subjects subject ON subject.subject_id = ticket.subject_id
        WHERE ticket.ticket_id = NEW.ticket_id
            AND ticket.subject_id = NEW.subject_id
            AND subject.kind = 'MANUAL_OPERATOR'
    ) THEN RAISE(ABORT, 'manual leg must belong to its ticket manual subject') END;
    SELECT CASE WHEN (NEW.command_id IS NULL) != (NEW.effect_operation_id IS NULL)
        OR (NEW.command_id IS NULL) != (NEW.order_ref IS NULL)
        THEN RAISE(ABORT, 'manual leg resources must be all null or one complete manual chain') END;
    SELECT CASE WHEN NEW.command_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM commands command
        JOIN effect_operations effect ON effect.effect_operation_id = NEW.effect_operation_id
        JOIN orders ord ON ord.order_ref = NEW.order_ref
        WHERE command.command_id = NEW.command_id
            AND command.subject_id = NEW.subject_id
            AND command.kind = 'manual_order'
            AND command.strategy_instance_id IS NULL
            AND command.run_id IS NULL
            AND effect.command_id = command.command_id
            AND effect.subject_id = NEW.subject_id
            AND effect.kind = 'MANUAL_ORDER'
            AND effect.strategy_instance_id IS NULL
            AND effect.run_id IS NULL
            AND ord.effect_operation_id = effect.effect_operation_id
            AND ord.role = 'MANUAL'
    ) THEN RAISE(ABORT, 'manual leg resources must be one chain in the ticket subject') END;
END;
"""


_MANUAL_CANCELLATION_IDENTITY_DDL = """\
CREATE TRIGGER trg_manual_order_cancellation_identity_immutable
BEFORE UPDATE OF order_ref, subject_id, cancel_request_id, command_id, effect_operation_id
ON manual_order_cancellations
BEGIN
    SELECT RAISE(ABORT, 'manual_order_cancellations identity is immutable');
END;
"""


_MANUAL_CANCELLATION_DELETE_DDL = """\
CREATE TRIGGER trg_manual_order_cancellation_delete_forbidden
BEFORE DELETE ON manual_order_cancellations
BEGIN
    SELECT RAISE(ABORT, 'manual_order_cancellations are append-only');
END;
"""


_MANUAL_CANCELLATION_SUBJECT_COMPATIBILITY_DDL = """\
CREATE TRIGGER trg_manual_order_cancellation_subject_compatible_insert
BEFORE INSERT ON manual_order_cancellations
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
        FROM manual_order_legs leg
        JOIN custody_subjects subject ON subject.subject_id = leg.subject_id
        JOIN commands command ON command.command_id = NEW.command_id
        JOIN effect_operations effect ON effect.effect_operation_id = NEW.effect_operation_id
        WHERE leg.order_ref = NEW.order_ref
            AND leg.subject_id = NEW.subject_id
            AND subject.kind = 'MANUAL_OPERATOR'
            AND command.subject_id = NEW.subject_id
            AND command.kind = 'manual_order'
            AND command.action = 'CANCEL_MANUAL_ORDER'
            AND command.strategy_instance_id IS NULL
            AND command.run_id IS NULL
            AND effect.command_id = command.command_id
            AND effect.subject_id = NEW.subject_id
            AND effect.kind = 'CANCEL'
            AND effect.strategy_instance_id IS NULL
            AND effect.run_id IS NULL
    ) THEN RAISE(ABORT, 'manual cancellation must own one manual ticket order') END;
END;
"""

MANUAL_CANCELLATION_SUBJECT_COMPATIBILITY_DDL = (
    _MANUAL_CANCELLATION_IDENTITY_DDL
    + _MANUAL_CANCELLATION_DELETE_DDL
    + _MANUAL_CANCELLATION_SUBJECT_COMPATIBILITY_DDL
)
MANUAL_CANCELLATION_SUBJECT_COMPATIBILITY_STATEMENTS: tuple[str, ...] = (
    _MANUAL_CANCELLATION_IDENTITY_DDL,
    _MANUAL_CANCELLATION_DELETE_DDL,
    _MANUAL_CANCELLATION_SUBJECT_COMPATIBILITY_DDL,
)
