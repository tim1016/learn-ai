"""Account-keyed real-paper and synthetic Clerk composition tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.broker.alpaca.clerk.account_authority import (
    AccountAuthorityIdentityError,
    bind_real_alpaca_ports,
    bind_synthetic_ports,
)
from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    ClerkAuthorityRegistry,
    activate_synthetic_clerk_authority,
    select_synthetic_clerk_runtime,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import (
    SealedAccountMismatchError,
    SqliteAlpacaClerkFacade,
)
from app.broker.alpaca.clerk.synthetic_broker import SyntheticBroker
from app.schemas.account_authority import AuthorityScopedRow, SingleAuthorityAggregate
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan


def test_opposite_account_namespaces_are_refused_when_ports_bind() -> None:
    synthetic = SyntheticBroker(account_id="sim:ema-1")

    with pytest.raises(AccountAuthorityIdentityError):
        bind_real_alpaca_ports(account_id="sim:ema-1", read=synthetic, trade=synthetic)
    with pytest.raises(AccountAuthorityIdentityError):
        bind_synthetic_ports(account_id="PA-REAL", read=synthetic, trade=synthetic)


async def test_synthetic_authority_requires_explicit_durable_activation(tmp_path: Path) -> None:
    account_id = "sim:ema-1"
    broker = SyntheticBroker(account_id=account_id)

    unavailable = await select_synthetic_clerk_runtime(
        account_id=account_id,
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
    )

    assert unavailable.clerk is None
    assert unavailable.startup_failure is not None
    assert unavailable.startup_failure.reason_code == "SYNTHETIC_ACTIVATION_REQUIRED"

    await activate_synthetic_clerk_authority(account_id=account_id, artifacts_root=tmp_path)
    runtime = await select_synthetic_clerk_runtime(
        account_id=account_id,
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
    )
    try:
        assert runtime.authority_kind == "synthetic"
        assert runtime.selected_account_authority_kind == "synthetic"
        assert runtime.selected_account_id == account_id
        assert runtime.sqlite_repository is not None
    finally:
        await runtime.close()


async def test_authority_registry_resolves_simultaneous_authorities_by_exact_account(
    tmp_path: Path,
) -> None:
    real_repo = ClerkSqliteRepository.initialize(account_id="PA-REAL", artifacts_root=tmp_path)
    real_broker = SyntheticBroker(account_id="sim:unused")
    real_facade = SqliteAlpacaClerkFacade(repo=real_repo, read=real_broker, trade=real_broker)
    real = ActiveClerkRuntime(
        authority_kind="sqlite",
        clerk=real_facade,
        account_id="PA-REAL",
        account_authority_kind="real_paper",
    )
    synthetic_account = "sim:ema-2"
    synthetic_broker = SyntheticBroker(account_id=synthetic_account)
    await activate_synthetic_clerk_authority(account_id=synthetic_account, artifacts_root=tmp_path)
    synthetic = await select_synthetic_clerk_runtime(
        account_id=synthetic_account,
        read=synthetic_broker,
        trade=synthetic_broker,
        artifacts_root=tmp_path,
    )
    registry = ClerkAuthorityRegistry()
    try:
        registry.register(real)
        registry.register(synthetic)

        assert registry.resolve("PA-REAL") is real
        assert registry.resolve(synthetic_account) is synthetic
        assert registry.resolve("PA-OTHER") is None
    finally:
        await synthetic.close()
        real_repo.close()


async def test_sealed_account_mismatch_happens_before_any_run_is_created(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="PA-REAL", artifacts_root=tmp_path)
    broker = SyntheticBroker(account_id="sim:unused")
    facade = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker)
    binding = BrokerBotBinding(
        strategy_instance_id="ema-1",
        broker="alpaca",
        symbol="SPY",
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-1",
        created_at_ms=1,
    )
    try:
        with pytest.raises(SealedAccountMismatchError) as error:
            await facade.register_strategy_run(binding, sealed_account_id="sim:ema-1")

        assert error.value.reason_code == "SEALED_ACCOUNT_MISMATCH"
        assert repo.strategy_instance("ema-1") is None
        assert repo.active_run("ema-1") is None
    finally:
        repo.close()


def test_single_authority_aggregate_rejects_mixed_real_and_synthetic_rows() -> None:
    with pytest.raises(ValidationError, match="one exact account authority"):
        SingleAuthorityAggregate(
            account_id="PA-REAL",
            authority_kind="real_paper",
            rows=(
                AuthorityScopedRow(account_id="PA-REAL", authority_kind="real_paper"),
                AuthorityScopedRow(account_id="sim:ema-1", authority_kind="synthetic"),
            ),
        )


@pytest.mark.parametrize(
    ("account_id", "authority_kind"),
    [
        ("sim:ema-1", "real_paper"),
        ("PA-REAL", "synthetic"),
    ],
)
def test_authority_scoped_rows_enforce_account_namespace_at_model_boundary(
    account_id: str,
    authority_kind: str,
) -> None:
    with pytest.raises(ValidationError, match="require"):
        AuthorityScopedRow(
            account_id=account_id,
            authority_kind=authority_kind,  # type: ignore[arg-type]
        )


def test_single_authority_aggregate_enforces_selected_account_namespace_when_empty() -> None:
    with pytest.raises(ValidationError, match="sim: account ids require synthetic authority"):
        SingleAuthorityAggregate(
            account_id="sim:ema-1",
            authority_kind="real_paper",
            rows=(),
        )
