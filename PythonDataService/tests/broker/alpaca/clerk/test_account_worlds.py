"""The four closed account worlds (ADR 0059 D1).

``sim:`` and ``shadow:`` are reserved namespaces; a real Alpaca port binds to
neither. ``real_live`` is never inferred from an account id — it is the
caller's positively-learned mode, so the kind derivation takes it as input.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.alpaca.broker import ALPACA_LIVE_CAPABILITIES
from app.broker.alpaca.clerk.account_authority import (
    SHADOW_ACCOUNT_PREFIX,
    SHADOW_EVIDENCE_ACCOUNT_PREFIX,
    AccountAuthorityIdentityError,
    authority_kind_for_account,
    bind_shadow_ports,
    custody_account_id_for,
    evidence_account_id_for,
    is_shadow_account_id,
    is_shadow_evidence_account_id,
    require_real_account_id,
    require_shadow_account_id,
    shadow_account_id_for_live_account,
    shadow_evidence_account_id_for_strategy,
)
from app.broker.alpaca.clerk.shadow_broker import NoSubmitAlpacaTradePort, compose_shadow_ports
from app.broker.contract.capabilities import BrokerCapabilities


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


def test_the_custody_id_a_world_holds_while_observing_a_real_account() -> None:
    """What an identity check must compare against, per world (ADR 0059 D2).

    The broker read answers the account it is pointed at; the projection
    answers the account the authority custodies. Under shadow those are
    deliberately different ids, and comparing the raw pair reads a correct
    shadow boot as a misconfiguration.
    """
    assert custody_account_id_for("real_paper", "PA0SANITIZED00001") == "PA0SANITIZED00001"
    assert custody_account_id_for("shadow", "9LIVE0001") == "shadow:9LIVE0001"
    assert custody_account_id_for("shadow", "9LIVE0001") != "9LIVE0001"
    with pytest.raises(AccountAuthorityIdentityError, match="reserved"):
        custody_account_id_for("shadow", "shadow:9LIVE0001")


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


class _LiveTradePort:
    """The live Alpaca port shape: a `submit` that would reach real money."""

    broker_id = "alpaca"

    def capabilities(self) -> BrokerCapabilities:
        return ALPACA_LIVE_CAPABILITIES

    async def get_account(self) -> None:  # pragma: no cover - never reached
        raise AssertionError("the live account read must not be reached")

    async def submit(self, leg: object, *, client_order_id: str) -> None:  # pragma: no cover
        raise AssertionError("LIVE TRADE PORT WAS REACHED")

    async def cancel(self, order_id: str) -> None:  # pragma: no cover
        raise AssertionError("LIVE CANCEL WAS REACHED")

    async def get_order_by_client_order_id(self, client_order_id: str) -> None:  # pragma: no cover
        return None


def test_a_shadow_composition_binds_only_the_no_submit_trade_port(tmp_path: Path) -> None:
    """The branch's whole safety claim, made unbypassable at the binding.

    ``bind_shadow_ports`` validated the account id and accepted any trade
    port, so passing the live ``AlpacaBroker`` still in scope at the shadow
    selector would type-check, lint clean and wire a real-money submit path.
    """
    ports = compose_shadow_ports(
        live_read=_LiveTradePort(),  # type: ignore[arg-type]
        live_account_id="9LIVE0001",
        artifacts_root=tmp_path,
    )

    bound = bind_shadow_ports(
        account_id=ports.account_id, read=ports.read, trade=ports.trade
    )
    assert isinstance(bound.trade, NoSubmitAlpacaTradePort)

    with pytest.raises(AccountAuthorityIdentityError, match="no-submit trade port"):
        bind_shadow_ports(
            account_id=ports.account_id,
            read=ports.read,
            trade=_LiveTradePort(),  # type: ignore[arg-type]
        )
