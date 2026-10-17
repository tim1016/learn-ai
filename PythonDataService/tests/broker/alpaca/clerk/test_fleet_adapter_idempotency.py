"""A mutating operation of the Alpaca catalog says how its repeats are made safe (#3216).

The provider refuses read idempotency on PUT, PATCH and DELETE but not on POST,
because a plan or a check is a POST that only reads. So a POST declared with no
idempotency at all used to take the read default and route as an unfenced read.
The catalog builder now has no default for anything that is not a GET.
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.fleet_adapter import ALPACA_OPERATIONS, _op
from app.broker.fleet.provider import Capability, OperationIdempotency


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_an_operation_that_is_not_a_get_must_declare_its_idempotency(method: str) -> None:
    with pytest.raises(ValueError, match="declare its idempotency"):
        _op("new_action", method, "/accounts/{account_id}/new-action", capability=Capability.BOT_ACTION, account=True)


def test_a_get_reads_without_a_declaration() -> None:
    operation = _op("new_read", "GET", "/accounts/{account_id}/new-read", capability=Capability.BOT_PANEL_READ, account=True)

    assert operation.idempotency is OperationIdempotency.READ


def test_the_posts_that_route_as_reads_are_exactly_the_plans_previews_and_checks() -> None:
    """A POST that is not fenced says so by name here; a new one is a reviewed addition to this set."""
    assert {
        operation.operation_id
        for operation in ALPACA_OPERATIONS
        if operation.method == "POST" and operation.idempotency is OperationIdempotency.READ
    } == {
        "bot_admission_plan",
        "bot_budget_preview",
        "bot_end_preview",
        "bot_option_structure_preview",
        "options_bot_review",
        "options_bot_resume_review",
        "paper_access_plan",
        "custody_recovery_check",
        "custody_bot_recovery_check",
        "manual_orders_preview",
    }
