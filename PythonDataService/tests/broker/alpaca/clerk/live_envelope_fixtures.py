"""The one envelope every composition test offers an authority (ADR 0059 D4).

Shared so a test that asserts an authority *carries* the envelope and one
that asserts a paper authority *refuses* it are comparing the same values.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeValues

TEST_ENVELOPE_VALUES = LiveEnvelopeValues(
    loss_fraction=0.05,
    loss_usd=5_000.0,
    shadow_sessions=1,
    arming_max_sessions=20,
    xh_entry_bps=10.0,
    xh_exit_bps=10.0,
)

__all__ = ["TEST_ENVELOPE_VALUES"]
