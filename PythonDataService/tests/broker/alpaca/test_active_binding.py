"""The binding's refusal is what decides the account's background work (#2669).

One decision, read by the boot that starts that work and by a reconnect whose
acknowledgement can refuse the binding after the boot started it: while the
binding stands the work runs; once it is refused the work does not.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.broker.alpaca.active_binding import (
    UnboundBroker,
    account_background_work_refused,
    refuse_active_alpaca_binding,
    reset_active_alpaca_binding_for_testing,
    set_active_alpaca_binding,
)


@pytest.fixture(autouse=True)
def _unbound() -> Iterator[None]:
    reset_active_alpaca_binding_for_testing()
    yield
    reset_active_alpaca_binding_for_testing()


class _Context:
    """The binding's runtime context shape; the decision never reads it."""


def test_an_unresolved_binding_has_not_refused_the_work() -> None:
    assert account_background_work_refused() is False


def test_a_bound_binding_has_not_refused_the_work() -> None:
    set_active_alpaca_binding(_Context())  # type: ignore[arg-type]

    assert account_background_work_refused() is False


def test_a_refused_binding_refuses_the_work() -> None:
    refuse_active_alpaca_binding(UnboundBroker(
        reason="account_pin_mismatch",
        message="The broker account no longer matches the approved configuration.",
        next_step="Restore its credentials or verify and apply a new revision.",
    ))

    assert account_background_work_refused() is True
