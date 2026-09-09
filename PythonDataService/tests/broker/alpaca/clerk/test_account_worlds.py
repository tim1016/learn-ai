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
    bind_real_alpaca_ports,
    bind_shadow_ports,
    custody_account_id_for,
    custody_account_ids_for,
    evidence_account_id_for,
    is_shadow_account_id,
    is_shadow_evidence_account_id,
    live_account_id_for_shadow_account,
    require_real_account_id,
    require_shadow_account_id,
    shadow_account_id_for_live_account,
    shadow_evidence_account_id_for_strategy,
)
from app.broker.alpaca.clerk.shadow_broker import NoSubmitAlpacaTradePort, compose_shadow_ports
from app.broker.contract.capabilities import BrokerCapabilities
from app.schemas.account_authority import world_admits_account_mode


def test_shadow_prefix_is_reserved_and_distinct_from_sim() -> None:
    assert SHADOW_ACCOUNT_PREFIX == "shadow:"
    assert is_shadow_account_id("shadow:9LIVE0001") is True
    assert is_shadow_account_id("sim:ema-1") is False
    assert is_shadow_account_id("PA0SANITIZED00001") is False


@pytest.mark.parametrize(
    "account_id",
    [
        "sim:ema-1",
        "shadow:9LIVE0001",
        # The two evidence namespaces are minted by this module too, so a
        # "real" account id carrying one is a composition bug. The guard read
        # as though it covered every reserved namespace and covered two.
        "paper:ema-1",
        "shadow-evidence:ema-1",
    ],
)
def test_real_ports_refuse_every_reserved_namespace(account_id: str) -> None:
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


def test_the_live_account_a_shadow_custody_id_observes_is_its_inverse() -> None:
    """The module that mints a prefix is the only one that strips it."""
    assert live_account_id_for_shadow_account("shadow:9LIVE0001") == "9LIVE0001"
    assert live_account_id_for_shadow_account("9LIVE0001") == "9LIVE0001"
    assert live_account_id_for_shadow_account(shadow_account_id_for_live_account("9LIVE0001")) == "9LIVE0001"


def test_the_two_custody_ids_of_one_live_account_are_both_admissible() -> None:
    """Shadow custody seals ``shadow:<id>``; slice 7's real_live custody seals ``<id>``."""
    assert custody_account_ids_for("9LIVE0001") == frozenset({"9LIVE0001", "shadow:9LIVE0001"})
    with pytest.raises(AccountAuthorityIdentityError, match="reserved"):
        custody_account_ids_for("shadow:9LIVE0001")


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


def test_each_world_admits_exactly_one_account_mode() -> None:
    """ADR 0059 D1 / slice 7 R8 #13: one closed table, no world admits two modes."""
    assert world_admits_account_mode("real_paper", "paper") is True
    assert world_admits_account_mode("real_paper", "live") is False
    assert world_admits_account_mode("shadow", "live") is True
    assert world_admits_account_mode("shadow", "paper") is False
    assert world_admits_account_mode("real_live", "live") is True
    assert world_admits_account_mode("real_live", "paper") is False
    assert world_admits_account_mode("real_live", None) is False


def test_real_ports_bind_the_live_world_only_when_told_the_learned_mode() -> None:
    live = bind_real_alpaca_ports(account_id="9LIVE0001", read=object(), trade=object(), account_mode="live")
    paper = bind_real_alpaca_ports(account_id="PA-TEST", read=object(), trade=object())
    assert live.authority_kind == "real_live"
    assert paper.authority_kind == "real_paper"


def test_a_live_instance_retains_its_bars_under_its_own_namespace() -> None:
    assert (
        evidence_account_id_for(mode="trade", strategy_instance_id="ema-live-1", custody_kind="real_live")
        == "live-evidence:ema-live-1"
    )
    with pytest.raises(AccountAuthorityIdentityError):
        require_real_account_id("live-evidence:ema-live-1")


@pytest.mark.parametrize(
    ("account_id", "world", "expected"),
    [
        ("PA-TEST", "real_paper", "real_paper"),
        ("shadow:9LIVE0001", "shadow", "shadow"),
        ("9LIVE0001", "real_live", "real_live"),
        # The id's own namespace wins over a world label that cannot apply to it.
        ("shadow:9LIVE0001", "real_live", "shadow"),
        ("sim:ema-1", "real_paper", "synthetic"),
    ],
)
def test_authority_kind_in_world_follows_the_world_never_a_default(account_id: str, world: str, expected: str) -> None:
    from app.broker.alpaca.clerk.account_authority import authority_kind_in_world

    assert authority_kind_in_world(account_id, world) == expected


@pytest.mark.parametrize(("world", "expected"), [("real_live", "live-evidence:ema-live-1"), ("real_paper", "paper:ema-live-1")])
def test_a_bindings_replay_ledger_is_read_where_the_primary_world_wrote_it(
    monkeypatch: pytest.MonkeyPatch, world: str, expected: str
) -> None:
    """R13: the writer (`PrimaryAccountBindingAuthority.source_bars`) and the reader name one namespace."""
    from app.services import run_replay_proof
    from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan

    monkeypatch.setattr(run_replay_proof, "primary_custody_world", lambda: world)
    binding = BrokerBotBinding(
        strategy_instance_id="ema-live-1",
        broker="alpaca",
        symbol="SPY",
        mode="trade",
        action_plan=alpaca_v1_action_plan("SPY"),
        sealed_account_id="9LIVE0001",
        run_id="ema-live-1-run-1",
        created_at_ms=1_757_000_000_000,
    )
    assert run_replay_proof.ledger_account_id_for(binding) == expected
