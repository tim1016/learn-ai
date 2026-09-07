"""The Alpaca live verdict is a pure function of settings and clerk selection
(ADR 0059 D8). It never contacts the broker and never guesses."""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    ClerkStartupFailure,
)
from app.broker.alpaca.config import AlpacaSettings
from app.services.alpaca_live_verdict import alpaca_live_verdict

_NOW = 1_800_000_000_000
_LIVE = {
    "live_loss_fraction": 0.02, "live_loss_usd": 500.0, "live_shadow_sessions": 5,
    "live_arming_max_sessions": 20, "live_xh_entry_bps": 10.0, "live_xh_exit_bps": 10.0,
}


def _paper() -> AlpacaSettings:
    return AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper")


def _live() -> AlpacaSettings:
    return AlpacaSettings(api_key_id="k", api_secret_key="s", mode="live", **_LIVE)


def _failure(reason_code: str, account_id: str | None) -> ActiveClerkRuntime:
    return ActiveClerkRuntime(
        authority_kind="unavailable",
        account_id=account_id,
        startup_failure=ClerkStartupFailure(
            reason_code=reason_code,
            account_id=account_id,
            scope="ACCOUNT_CLERK",
            impact="x",
            recovery="y",
            observed_at_ms=_NOW,
        ),
    )


def test_unconfigured_settings_is_unknown() -> None:
    verdict = alpaca_live_verdict(settings=None, runtime=None, now_ms=_NOW)

    assert verdict.configured_mode == "unconfigured"
    assert verdict.final_verdict == "unknown"
    assert verdict.observed_at_ms == _NOW


def test_paper_settings_is_paper_regardless_of_clerk_state() -> None:
    verdict = alpaca_live_verdict(settings=_paper(), runtime=None, now_ms=_NOW)

    assert verdict.configured_mode == "paper"
    assert verdict.final_verdict == "paper"
    assert verdict.envelope_state == "not_applicable"
    assert verdict.armed_instance_count == 0


def test_live_with_refused_clerk_is_live_unarmed_and_names_the_account() -> None:
    runtime = _failure("LIVE_ACCOUNT_REFUSED", "9LIVE0001")

    verdict = alpaca_live_verdict(settings=_live(), runtime=runtime, now_ms=_NOW)

    assert verdict.configured_mode == "live"
    assert verdict.observed_account_id == "9LIVE0001"
    assert verdict.mode_agreement == "agreed"
    assert verdict.clerk_refusal_reason_code == "LIVE_ACCOUNT_REFUSED"
    assert verdict.envelope_state == "configured_unsealed"
    assert verdict.final_verdict == "live-unarmed"
    assert "9LIVE0001" in verdict.headline


def test_live_with_mode_disagreement_is_unknown_and_disagreed() -> None:
    runtime = _failure("LIVE_MODE_DISAGREEMENT", None)

    verdict = alpaca_live_verdict(settings=_live(), runtime=runtime, now_ms=_NOW)

    assert verdict.mode_agreement == "disagreed"
    assert verdict.final_verdict == "unknown"


def test_live_with_no_account_observed_is_unknown_and_unobserved() -> None:
    runtime = _failure("BROKER_ACCOUNT_UNAVAILABLE", None)

    verdict = alpaca_live_verdict(settings=_live(), runtime=runtime, now_ms=_NOW)

    assert verdict.mode_agreement == "unobserved"
    assert verdict.final_verdict == "unknown"


def test_live_with_no_runtime_yet_is_unknown() -> None:
    verdict = alpaca_live_verdict(settings=_live(), runtime=None, now_ms=_NOW)

    assert verdict.clerk_authority == "not_installed"
    assert verdict.final_verdict == "unknown"


@pytest.mark.parametrize("final", ["paper", "live-unarmed", "unknown"])
def test_every_verdict_carries_server_authored_copy(final: str) -> None:
    settings, runtime = {
        "paper": (_paper(), None),
        "live-unarmed": (_live(), _failure("LIVE_ACCOUNT_REFUSED", "9LIVE0001")),
        "unknown": (None, None),
    }[final]

    verdict = alpaca_live_verdict(settings=settings, runtime=runtime, now_ms=_NOW)

    assert verdict.final_verdict == final
    assert verdict.headline and verdict.detail


def test_paper_with_mode_disagreement_is_unknown_not_reassuring() -> None:
    runtime = _failure("LIVE_MODE_DISAGREEMENT", None)

    verdict = alpaca_live_verdict(settings=_paper(), runtime=runtime, now_ms=_NOW)

    assert verdict.configured_mode == "paper"
    assert verdict.mode_agreement == "disagreed"
    assert verdict.final_verdict == "unknown"
    assert "no real money" not in verdict.headline
    assert verdict.clerk_refusal_reason_code == "LIVE_MODE_DISAGREEMENT"


def test_clean_paper_selection_is_the_normal_path() -> None:
    runtime = ActiveClerkRuntime(authority_kind="sqlite", account_id="PA0SANITIZED00001")

    verdict = alpaca_live_verdict(settings=_paper(), runtime=runtime, now_ms=_NOW)

    assert verdict.clerk_authority == "sqlite"
    assert verdict.clerk_refusal_reason_code is None
    assert verdict.mode_agreement == "agreed"
    assert verdict.final_verdict == "paper"
    assert "PA0SANITIZED00001" in verdict.headline


def test_clerk_authority_literal_tracks_the_runtime_kind() -> None:
    from typing import get_args

    from app.broker.alpaca.clerk.active_authority import AuthorityKind as RuntimeAuthorityKind
    from app.schemas.alpaca_live_verdict import ClerkAuthority

    assert set(get_args(ClerkAuthority)) == set(get_args(RuntimeAuthorityKind)) | {"not_installed"}


def test_observed_at_ms_is_bounded_to_the_canonical_epoch_range() -> None:
    from pydantic import ValidationError

    from app.schemas.alpaca_live_verdict import AlpacaLiveVerdict
    from app.utils.session_anchors import MAX_TIMESTAMP_MS

    base = alpaca_live_verdict(settings=_paper(), runtime=None, now_ms=_NOW).model_dump()

    AlpacaLiveVerdict(**{**base, "observed_at_ms": MAX_TIMESTAMP_MS})
    with pytest.raises(ValidationError):
        AlpacaLiveVerdict(**{**base, "observed_at_ms": MAX_TIMESTAMP_MS + 1})
