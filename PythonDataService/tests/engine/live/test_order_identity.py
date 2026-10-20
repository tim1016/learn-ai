"""Module A (order_identity) unit tests — ADR-0008 §1 / PRD #446 test plan A.

Pure identity assertions, exact (no tolerance). Covers: round-trip
mint->build->parse; unambiguous final-colon split; exact-namespace ownership
(the /v10-vs-/v1 prefix-collision case); dual-read; equality-of-components;
length guard; the ownership ladder and order_id-never-owns.
"""

from __future__ import annotations

import base64
from collections.abc import Callable

import pytest

from app.engine.live.order_identity import (
    DEFAULT_ORDER_REF_MAX_LENGTH,
    INTENT_ID_LEN,
    ORDER_REF_FIXED_OVERHEAD,
    InstanceIdTooLongError,
    OrderRefParseError,
    OrderRefTooLongError,
    OwnershipRung,
    build_bot_order_namespace,
    build_manual_order_namespace,
    build_order_ref,
    classify_ownership,
    max_strategy_instance_id_len,
    mint_intent_id,
    order_ref_namespace_matches,
    parse_order_ref,
    validate_broker_owned_instance_id,
)


def test_mint_intent_id_is_22_char_base64url() -> None:
    iid = mint_intent_id()
    assert len(iid) == INTENT_ID_LEN == 22
    assert "/" not in iid and ":" not in iid
    assert len(base64.urlsafe_b64decode(iid + "==")) == 16


def test_mint_intent_id_unique() -> None:
    assert len({mint_intent_id() for _ in range(1000)}) == 1000


@pytest.mark.parametrize(
    "build_namespace,instance_id,expected_namespace",
    [
        pytest.param(build_bot_order_namespace, "spy_rsi", "learn-ai/spy_rsi/v1", id="bot_namespace"),
        pytest.param(build_manual_order_namespace, "operator", "manual/operator/v1", id="manual_namespace"),
    ],
)
def test_build_parse_round_trip(
    build_namespace: Callable[[str], str], instance_id: str, expected_namespace: str
) -> None:
    ns = build_namespace(instance_id)
    assert ns == expected_namespace, "the namespace builder mints the expected namespace"
    iid = mint_intent_id()
    ref = build_order_ref(ns, iid)
    assert parse_order_ref(ref) == (ns, iid), "mint -> build -> parse returns the namespace and intent id"


def test_parse_splits_on_final_colon_with_base64url_dashes() -> None:
    # base64url tokens use '-'/'_'; force one so the split is provably on the
    # final ':' and not confused by delimiter-adjacent characters.
    iid = base64.urlsafe_b64encode(b"\xff\xff" + b"\x00" * 14).rstrip(b"=").decode()
    assert "-" in iid or "_" in iid
    ns = "learn-ai/foo/v1"
    ref = build_order_ref(ns, iid)
    assert parse_order_ref(ref) == (ns, iid)


def test_order_ref_namespace_matches() -> None:
    allowed = {"learn-ai/foo/v1"}
    assert order_ref_namespace_matches(
        "learn-ai/foo/v1:" + mint_intent_id(), allowed
    ), "exact namespace match: the v1 ref is owned"
    # startswith would WRONGLY match this; exact equality must not.
    assert not order_ref_namespace_matches(
        "learn-ai/foo/v10:" + mint_intent_id(), allowed
    ), "exact namespace match: the /v10 ref is not owned by /v1 (version prefix collision)"

    dual_read = {"learn-ai/foo/v1", "learn-ai/foo/v2"}
    assert order_ref_namespace_matches(
        "learn-ai/foo/v1:" + mint_intent_id(), dual_read
    ), "dual read: the allowed set owns v1"
    assert order_ref_namespace_matches(
        "learn-ai/foo/v2:" + mint_intent_id(), dual_read
    ), "dual read: the allowed set owns v2"
    assert not order_ref_namespace_matches(
        "learn-ai/foo/v3:" + mint_intent_id(), dual_read
    ), "dual read: v3 is not in the allowed set"

    assert not order_ref_namespace_matches(None, allowed), "a None order_ref is not owned"
    assert not order_ref_namespace_matches("no-colon-here", allowed), "an unparseable ref with no colon is not owned"
    assert not order_ref_namespace_matches(":empty-ns", allowed), "an unparseable ref with an empty namespace is not owned"


def test_parse_rejects_malformed() -> None:
    with pytest.raises(OrderRefParseError):
        parse_order_ref("no-delimiter")
    with pytest.raises(OrderRefParseError):
        parse_order_ref("ns:")
    with pytest.raises(OrderRefParseError):
        parse_order_ref(":iid")


def test_build_order_ref_fails_closed_over_cap() -> None:
    ns = build_bot_order_namespace("x" * 200)
    with pytest.raises(OrderRefTooLongError):
        build_order_ref(ns, mint_intent_id(), max_length=DEFAULT_ORDER_REF_MAX_LENGTH)


def test_instance_id_length_rule_is_cap_minus_overhead() -> None:
    assert ORDER_REF_FIXED_OVERHEAD == 35
    assert max_strategy_instance_id_len(60) == 25
    assert validate_broker_owned_instance_id("a" * 25, order_ref_max_length=60) == "a" * 25
    with pytest.raises(InstanceIdTooLongError):
        validate_broker_owned_instance_id("a" * 26, order_ref_max_length=60)


def test_order_ref_at_max_len_sid_builds_to_exactly_cap() -> None:
    sid = "a" * max_strategy_instance_id_len(60)
    ns = build_bot_order_namespace(sid)
    ref = build_order_ref(ns, mint_intent_id(), max_length=60)
    assert len(ref) == 60


def test_classify_ownership_ladder() -> None:
    allowed = {"learn-ai/foo/v1"}
    iid = mint_intent_id()
    assert (
        classify_ownership(
            order_ref=f"learn-ai/foo/v1:{iid}", perm_id=None, exec_id=None,
            allowed_namespaces=allowed, known_intent_ids=set(),
            known_perm_ids=set(), known_exec_ids=set(),
        )
        is OwnershipRung.NAMESPACE
    )
    # foreign-namespace ref but a known intent_id -> INTENT_ID rung
    assert (
        classify_ownership(
            order_ref=f"learn-ai/other/v1:{iid}", perm_id=None, exec_id=None,
            allowed_namespaces=allowed, known_intent_ids={iid},
            known_perm_ids=set(), known_exec_ids=set(),
        )
        is OwnershipRung.INTENT_ID
    )
    assert (
        classify_ownership(
            order_ref=None, perm_id=42, exec_id=None,
            allowed_namespaces=allowed, known_intent_ids=set(),
            known_perm_ids={42}, known_exec_ids=set(),
        )
        is OwnershipRung.PERM_ID
    )
    assert (
        classify_ownership(
            order_ref=None, perm_id=None, exec_id="e1",
            allowed_namespaces=allowed, known_intent_ids=set(),
            known_perm_ids=set(), known_exec_ids={"e1"},
        )
        is OwnershipRung.EXEC_ID
    )
    assert (
        classify_ownership(
            order_ref=None, perm_id=None, exec_id=None,
            allowed_namespaces=allowed, known_intent_ids=set(),
            known_perm_ids=set(), known_exec_ids=set(),
        )
        is OwnershipRung.NONE
    )


def test_order_id_alone_never_proves_ownership() -> None:
    # order_id is not even a parameter; a foreign order with unknown
    # ref/perm/exec is NONE regardless of any matching session order_id.
    allowed = {"learn-ai/foo/v1"}
    assert (
        classify_ownership(
            order_ref="learn-ai/foreign/v1:" + mint_intent_id(),
            perm_id=999, exec_id="foreign-exec",
            allowed_namespaces=allowed, known_intent_ids=set(),
            known_perm_ids=set(), known_exec_ids=set(),
        )
        is OwnershipRung.NONE
    )
