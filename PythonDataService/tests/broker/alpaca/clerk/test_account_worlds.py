"""The four closed account worlds (ADR 0059 D1).

``sim:`` and ``shadow:`` are reserved namespaces; a real Alpaca port binds to
neither. ``real_live`` is never inferred from an account id — it is the
caller's positively-learned mode, so the kind derivation takes it as input.
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.account_authority import (
    SHADOW_ACCOUNT_PREFIX,
    SHADOW_EVIDENCE_ACCOUNT_PREFIX,
    AccountAuthorityIdentityError,
    authority_kind_for_account,
    evidence_account_id_for,
    is_shadow_account_id,
    is_shadow_evidence_account_id,
    require_real_account_id,
    require_shadow_account_id,
    shadow_account_id_for_live_account,
    shadow_evidence_account_id_for_strategy,
)


def test_shadow_prefix_is_reserved_and_distinct_from_sim() -> None:
    assert SHADOW_ACCOUNT_PREFIX == "shadow:"
    assert is_shadow_account_id("shadow:9LIVE0001") is True
    assert is_shadow_account_id("sim:ema-1") is False
    assert is_shadow_account_id("PA0SANITIZED00001") is False


@pytest.mark.parametrize("account_id", ["sim:ema-1", "shadow:9LIVE0001"])
def test_real_ports_refuse_both_reserved_namespaces(account_id: str) -> None:
    with pytest.raises(AccountAuthorityIdentityError, match="reserved"):
        require_real_account_id(account_id)


def test_real_account_id_passes_paper_and_live_shapes() -> None:
    assert require_real_account_id("PA0SANITIZED00001") == "PA0SANITIZED00001"
    assert require_real_account_id("9LIVE0001") == "9LIVE0001"


def test_shadow_requires_the_shadow_namespace_and_names_one_account() -> None:
    assert require_shadow_account_id("shadow:9LIVE0001") == "shadow:9LIVE0001"
    with pytest.raises(AccountAuthorityIdentityError, match="shadow: account identity"):
        require_shadow_account_id("PA0SANITIZED00001")
    with pytest.raises(AccountAuthorityIdentityError, match="name one account"):
        require_shadow_account_id("shadow:")


def test_authority_kind_takes_mode_as_input_never_from_shape() -> None:
    assert authority_kind_for_account("sim:ema-1") == "synthetic"
    assert authority_kind_for_account("shadow:9LIVE0001") == "shadow"
    assert authority_kind_for_account("PA0SANITIZED00001") == "real_paper"
    # A live-shaped id under the default (paper) mode is still real_paper: the
    # shape never grants live. Only the caller's positive mode does.
    assert authority_kind_for_account("9LIVE0001") == "real_paper"
    assert authority_kind_for_account("9LIVE0001", account_mode="live") == "real_live"


def test_shadow_account_id_derives_from_the_live_account() -> None:
    assert shadow_account_id_for_live_account("9LIVE0001") == "shadow:9LIVE0001"
    with pytest.raises(AccountAuthorityIdentityError, match="reserved"):
        shadow_account_id_for_live_account("sim:ema-1")


def test_shadow_evidence_namespace_is_instance_scoped_and_not_a_custody_namespace() -> None:
    account_id = shadow_evidence_account_id_for_strategy("bot-a")
    assert account_id == f"{SHADOW_EVIDENCE_ACCOUNT_PREFIX}bot-a"
    assert is_shadow_evidence_account_id(account_id) is True
    assert is_shadow_account_id(account_id) is False
    assert is_shadow_evidence_account_id("shadow:9LIVE0001") is False


def test_evidence_account_id_follows_mode_then_custody_world() -> None:
    assert evidence_account_id_for(mode="dry_run", strategy_instance_id="b", custody_kind="real_paper") == "sim:b"
    assert evidence_account_id_for(mode="trade", strategy_instance_id="b", custody_kind="real_paper") == "paper:b"
    assert evidence_account_id_for(mode="trade", strategy_instance_id="b", custody_kind="shadow") == "shadow-evidence:b"
    # log_only retains into the world's instance namespace exactly as the
    # primary binding authority always did; replay refuses the mode itself.
    assert evidence_account_id_for(mode="log_only", strategy_instance_id="b", custody_kind="real_paper") == "paper:b"
