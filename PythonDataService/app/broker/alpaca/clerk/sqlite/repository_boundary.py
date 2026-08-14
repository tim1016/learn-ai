"""Explicit external-writer boundary for the SQLite Clerk repository.

The repository's write coordinator makes one mutation atomic, but it cannot
make an arbitrary caller's read-decide-append sequence coherent.  Production
code outside this package therefore has a small, enumerated set of writer
entrances.  The accompanying AST test rejects an unclassified entrance.

Pure display and projection reads remain unfenced: they neither call a broker
nor mutate custody.  A safety or authorization projection must instead use a
single SQLite read transaction or carry and revalidate its control revision;
it must not take intake merely to make an ordinary display eventually current.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RepositoryWriterClassification(StrEnum):
    """The only permitted external paths to SQLite Clerk mutation."""

    ATOMIC = "atomic_repository_mutation"
    REVISION_BOUND_ATOMIC = "revision_bound_atomic_mutation"
    FACADE_WORKFLOW = "facade_bounded_workflow"


@dataclass(frozen=True)
class ExternalRepositoryWriter:
    """One production writer call site and the concurrency rule it satisfies."""

    path: str
    owner: str
    call: str
    classification: RepositoryWriterClassification
    rationale: str


REPOSITORY_MUTATION_METHODS = frozenset(
    {
        "append_transition",
        "append_execution_slice_if_absent",
        "observe_account_hold",
        "register_strategy_instance",
    }
)
REPOSITORY_MUTATION_HELPERS = frozenset(
    {
        "acknowledge_external_order",
        "fold_order_acknowledgement",
        "fold_order_evidence",
        "observe_external_order",
        "submit_start_run",
        "submit_stop_run",
    }
)
FACADE_WORKFLOW_METHODS = frozenset({"reconcile_account"})
FACADE_WORKFLOW_HELPERS = frozenset({"execute_recovery_action"})

EXTERNAL_REPOSITORY_WRITER_CENSUS = (
    ExternalRepositoryWriter(
        path="app/broker/alpaca/clerk/trade_evidence.py",
        owner="SqliteTradeUpdateEvidenceSink.record_lifecycle_event",
        call="append_execution_slice_if_absent",
        classification=RepositoryWriterClassification.FACADE_WORKFLOW,
        rationale="The active-authority evidence ingress holds intake for its bounded local read-decide-fold segment.",
    ),
    ExternalRepositoryWriter(
        path="app/broker/alpaca/clerk/trade_evidence.py",
        owner="SqliteTradeUpdateEvidenceSink.record_lifecycle_event",
        call="fold_order_acknowledgement",
        classification=RepositoryWriterClassification.FACADE_WORKFLOW,
        rationale="The acknowledgement fold is part of the same bounded evidence-ingress segment.",
    ),
    ExternalRepositoryWriter(
        path="app/broker/alpaca/clerk/trade_evidence.py",
        owner="SqliteTradeUpdateEvidenceSink.record_lifecycle_event",
        call="fold_order_evidence",
        classification=RepositoryWriterClassification.FACADE_WORKFLOW,
        rationale="Cumulative recovery is admitted only by the bounded evidence-ingress segment.",
    ),
    ExternalRepositoryWriter(
        path="app/broker/alpaca/clerk/trade_evidence.py",
        owner="SqliteTradeUpdateEvidenceSink.record_lifecycle_event",
        call="observe_account_hold",
        classification=RepositoryWriterClassification.FACADE_WORKFLOW,
        rationale="The missing-evidence hold is a local branch of the bounded evidence-ingress segment.",
    ),
    ExternalRepositoryWriter(
        path="app/broker/alpaca/clerk/trade_evidence.py",
        owner="SqliteTradeUpdateEvidenceSink.record_lifecycle_event",
        call="observe_external_order",
        classification=RepositoryWriterClassification.FACADE_WORKFLOW,
        rationale="Foreign-order observation is a local evidence fold under the shared intake boundary.",
    ),
    ExternalRepositoryWriter(
        path="app/broker/alpaca/clerk/trade_evidence.py",
        owner="SqliteTradeUpdateEvidenceSink.reconcile_gap",
        call="reconcile_account",
        classification=RepositoryWriterClassification.FACADE_WORKFLOW,
        rationale="Reconnect reconciliation delegates to the active facade instead of receiving raw repository and broker ports.",
    ),
    ExternalRepositoryWriter(
        path="app/routers/alpaca_clerk_sqlite.py",
        owner="reconcile_now",
        call="reconcile_account",
        classification=RepositoryWriterClassification.FACADE_WORKFLOW,
        rationale="The HTTP route verifies broker account identity, then delegates the full write workflow to the active facade.",
    ),
    ExternalRepositoryWriter(
        path="app/routers/alpaca_clerk_sqlite.py",
        owner="_execute_presented_recovery_action",
        call="execute_recovery_action",
        classification=RepositoryWriterClassification.FACADE_WORKFLOW,
        rationale="The HTTP recovery dispatcher receives only the active facade; its coverage-resolution branch revalidates the expected control revision inside one repository call.",
    ),
    ExternalRepositoryWriter(
        path="app/services/bot_trade_strategy.py",
        owner="_append_decision_receipt",
        call="append_decision_receipt",
        classification=RepositoryWriterClassification.ATOMIC,
        rationale="SqliteDecisionReceipts appends one decision receipt under the repository write coordinator and does not advance custody control revision.",
    ),
    ExternalRepositoryWriter(
        path="app/services/broker_v2_panel/sqlite_panel_source.py",
        owner="execute_sqlite_panel_action",
        call="execute_recovery_action",
        classification=RepositoryWriterClassification.FACADE_WORKFLOW,
        rationale="The panel recovery dispatcher passes the active facade through the same typed recovery-action boundary as HTTP.",
    ),
    ExternalRepositoryWriter(
        path="app/services/alpaca_sqlite_synthetic_drill_support.py",
        owner="new_repo",
        call="register_strategy_instance",
        classification=RepositoryWriterClassification.ATOMIC,
        rationale="Synthetic setup registers one instance through the repository's write coordinator.",
    ),
    ExternalRepositoryWriter(
        path="app/services/alpaca_sqlite_synthetic_drill_support.py",
        owner="new_repo",
        call="submit_start_run",
        classification=RepositoryWriterClassification.ATOMIC,
        rationale="Synthetic setup admits one run through commit_first_transition's atomic idempotency path.",
    ),
    ExternalRepositoryWriter(
        path="app/services/sqlite_clerk_transaction_projection.py",
        owner="sqlite_acknowledge_external_order",
        call="acknowledge_external_order",
        classification=RepositoryWriterClassification.ATOMIC,
        rationale="The external-order helper performs its read-check-append under one repository write coordinator.",
    ),
)


__all__ = [
    "EXTERNAL_REPOSITORY_WRITER_CENSUS",
    "FACADE_WORKFLOW_HELPERS",
    "FACADE_WORKFLOW_METHODS",
    "REPOSITORY_MUTATION_HELPERS",
    "REPOSITORY_MUTATION_METHODS",
    "ExternalRepositoryWriter",
    "RepositoryWriterClassification",
]
