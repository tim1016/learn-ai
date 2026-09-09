"""The confirmation ceremony `cutover.py` invented and arming reuses (ADR 0059 D3)."""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.ceremony import (
    DEFAULT_CONFIRMATION_TTL_MS,
    MAX_CONFIRMATION_TTL_MS,
    plan_content_token,
    require_confirmation_ttl_ms,
    require_plan_token,
    require_unexpired,
)


class _Refused(ValueError):
    """A caller's own refusal type, exactly as `CutoverRefused` is."""


PAYLOAD = {"schema_version": 1, "account_id": "9LIVE0001", "created_at_ms": 10, "expires_at_ms": 20}


def test_the_bounds_are_the_ones_cutover_shipped() -> None:
    assert (DEFAULT_CONFIRMATION_TTL_MS, MAX_CONFIRMATION_TTL_MS) == (120_000, 300_000)


def test_the_ttl_must_be_a_whole_millisecond_count_inside_the_bound() -> None:
    assert require_confirmation_ttl_ms(1, refused=_Refused) == 1
    assert require_confirmation_ttl_ms(MAX_CONFIRMATION_TTL_MS, refused=_Refused) == MAX_CONFIRMATION_TTL_MS
    for bad in (0, -1, MAX_CONFIRMATION_TTL_MS + 1, True, 1.0):
        with pytest.raises(_Refused, match=r"confirmation TTL must be within 1\.\.300000 ms"):
            require_confirmation_ttl_ms(bad, refused=_Refused)  # type: ignore[arg-type]


def test_the_digest_of_a_fixed_payload_is_pinned_to_a_literal() -> None:
    """The bytes that are hashed are part of the contract, not an implementation detail.

    Every cutover plan token an operator is holding was minted over
    ``canonical_json_bytes`` -- canonical JSON *with* its trailing newline.
    Changing what is hashed would invalidate all of them silently, so the
    digest of one fixed payload is written out here once.
    """
    assert plan_content_token(PAYLOAD) == "60b1fa54852b30dab78968a50164f76b33440b658802a704c355b5afa86c32b3"


def test_the_token_is_the_payloads_own_canonical_digest_and_is_key_order_free() -> None:
    token = plan_content_token(PAYLOAD)
    assert len(token) == 64
    assert plan_content_token(dict(reversed(list(PAYLOAD.items())))) == token
    assert plan_content_token({**PAYLOAD, "created_at_ms": 11}) != token


def test_a_plan_that_does_not_hash_to_its_own_ids_is_refused_before_the_token_is_compared() -> None:
    token = plan_content_token(PAYLOAD)
    with pytest.raises(_Refused, match="cutover plan content hash does not verify"):
        require_plan_token(
            PAYLOAD,
            plan_id="0" * 64,
            confirmation_token=token,
            supplied_token=token,
            refused=_Refused,
            label="cutover",
        )
    with pytest.raises(_Refused, match="cutover plan content hash does not verify"):
        require_plan_token(
            PAYLOAD,
            plan_id=token,
            confirmation_token="0" * 64,
            supplied_token=token,
            refused=_Refused,
            label="cutover",
        )


def test_a_quoted_token_that_is_not_the_plans_is_refused_by_name() -> None:
    token = plan_content_token(PAYLOAD)
    require_plan_token(
        PAYLOAD, plan_id=token, confirmation_token=token, supplied_token=token, refused=_Refused, label="live arming"
    )
    with pytest.raises(_Refused, match="live arming confirmation token does not match the plan"):
        require_plan_token(
            PAYLOAD,
            plan_id=token,
            confirmation_token=token,
            supplied_token="0" * 64,
            refused=_Refused,
            label="live arming",
        )


@pytest.mark.parametrize(
    "supplied",
    [
        pytest.param("töken", id="non-ascii"),
        pytest.param("0" * 63, id="too-short"),
        pytest.param("0" * 65, id="too-long"),
        pytest.param("A" * 64, id="uppercase-hex"),
        pytest.param("", id="empty"),
    ],
)
def test_a_token_that_cannot_be_a_digest_is_refused_by_shape_not_by_traceback(supplied: str) -> None:
    """``secrets.compare_digest`` raises ``TypeError`` on a non-ASCII ``str``.

    An operator's mistyped token must leave as the caller's refusal, so both
    CLIs sharing this helper keep their "one JSON object per invocation"
    contract.
    """
    token = plan_content_token(PAYLOAD)
    with pytest.raises(_Refused, match="cutover confirmation token does not match the plan"):
        require_plan_token(
            PAYLOAD,
            plan_id=token,
            confirmation_token=token,
            supplied_token=supplied,
            refused=_Refused,
            label="cutover",
        )


def test_expiry_is_inclusive_of_the_last_admissible_millisecond() -> None:
    require_unexpired(now_ms=20, expires_at_ms=20, refused=_Refused, label="cutover")
    with pytest.raises(_Refused, match="cutover confirmation token has expired"):
        require_unexpired(now_ms=21, expires_at_ms=20, refused=_Refused, label="cutover")


def test_the_refusal_type_is_the_callers_own() -> None:
    """`LiveArmingRefused` takes (reason_code, message), so the seam is a callable."""

    class _Coded(ValueError):
        def __init__(self, reason_code: str, message: str) -> None:
            super().__init__(message)
            self.reason_code = reason_code

    with pytest.raises(_Coded) as caught:
        require_unexpired(
            now_ms=21,
            expires_at_ms=20,
            refused=lambda message: _Coded("LIVE_ARMING_PLAN_EXPIRED", message),
            label="live arming",
        )
    assert caught.value.reason_code == "LIVE_ARMING_PLAN_EXPIRED"
