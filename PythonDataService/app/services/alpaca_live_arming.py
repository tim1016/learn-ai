"""Online transport around the same plan/reobserve/append ceremony as the CLI."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from app.broker.alpaca.active_binding import BrokerUnbound
from app.broker.alpaca.clerk.active_authority import primary_custody_world
from app.broker.alpaca.clerk.live_arming import LIVE_ARMING_INPUTS_CHANGED, LiveArmingRefused, latest_arming
from app.broker.alpaca.clerk.live_arming_ceremony import (
    LiveArmingPlan,
    account_arming,
    apply_arming,
    arming_accounts,
    configured_envelope,
    disarm,
    normalize_ceremony_root,
    plan_arming,
)
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.sqlite.operational_files import atomic_write_json
from app.broker.alpaca.paths import resolve_contained_path, safe_path_component
from app.broker.ibkr.config import live_artifacts_root
from app.broker_configuration.cli_binding import (
    EffectiveBroker,
    arming_configuration_handover,
    effective_broker,
    require_arming_the_effective_revision,
)
from app.broker_configuration.errors import BrokerConfigurationError
from app.broker_configuration.runtime import resolve_clerk_dir
from app.schemas.alpaca_live_arming import ArmingPlanView, ArmingStatusView
from app.utils.timestamps import Clock, now_ms_utc

logger = logging.getLogger(__name__)


class AlpacaLiveArmingService:
    def __init__(
        self,
        *,
        resolve: Callable[[], EffectiveBroker] = effective_broker,
        live_state_root: Path | None = None,
        artifacts_root: Path | None = None,
        clock: Clock = now_ms_utc,
    ) -> None:
        self._resolve = resolve
        self._live_root = normalize_ceremony_root(live_state_root or live_artifacts_root())
        self._clock = clock
        self._artifacts_root = normalize_ceremony_root(artifacts_root or resolve_clerk_dir())

    def _plan_path(self, root: Path, plan_id: str) -> Path:
        return resolve_contained_path(root, "live-arming-plans", safe_path_component(plan_id, "plan id") + ".json")

    def prepare(self, account_id: str, sid: str) -> ArmingPlanView:
        with arming_configuration_handover():
            resolved = self._resolve()
            require_arming_the_effective_revision(resolved.selection)
            root = normalize_ceremony_root(resolved.settings.clerk_dir)
            plan = plan_arming(
                strategy_instance_id=sid,
                artifacts_root=root,
                live_state_root=self._live_root,
                settings=resolved.settings,
                clock=self._clock,
            )
            self._require_account(account_id, plan.live_account_id)
            previous = latest_arming(LiveArmingLedger(root, live_account_id=account_id).records_for(sid))
            changes = []
            prior_envelope = {} if previous is None else previous.envelope_values
            prior_terms = {} if previous is None else (previous.exit_terms or {})
            for section, current, prior in (
                ("Envelope", plan.envelope_values, prior_envelope),
                ("Exit terms", plan.exit_terms or {}, prior_terms),
            ):
                for key, value in current.items():
                    if prior.get(key) != value:
                        changes.append(f"{section}: {key.replace('_', ' ')} · {prior.get(key, 'unset')} → {value}")
            atomic_write_json(self._plan_path(root, plan.plan_id), asdict(plan))
            return ArmingPlanView(
                plan_id=plan.plan_id,
                confirmation_token=plan.confirmation_token,
                account_id=account_id,
                strategy_instance_id=sid,
                created_at_ms=plan.created_at_ms,
                expires_at_ms=plan.expires_at_ms,
                envelope=plan.envelope_values,
                exit_terms=plan.exit_terms,
                changes=tuple(changes),
                shadow_receipt_sha256=plan.shadow_receipt_sha256,
            )

    def apply(self, account_id: str, sid: str, *, plan_id: str, confirmation_token: str) -> ArmingStatusView:
        with arming_configuration_handover():
            resolved = self._resolve()
            require_arming_the_effective_revision(resolved.selection)
            root = normalize_ceremony_root(resolved.settings.clerk_dir)
            plan = LiveArmingPlan.from_payload(json.loads(self._plan_path(root, plan_id).read_text()))
            self._require_account(account_id, plan.live_account_id)
            if plan.strategy_instance_id != sid:
                raise LiveArmingRefused(LIVE_ARMING_INPUTS_CHANGED, "The plan belongs to another bot.")
            apply_arming(
                plan=plan,
                confirmation_token=confirmation_token,
                artifacts_root=root,
                live_state_root=self._live_root,
                settings=resolved.settings,
                clock=self._clock,
            )
        return self.status(account_id, sid)

    def status(self, account_id: str, sid: str) -> ArmingStatusView:
        resolved = self._resolve()
        root = normalize_ceremony_root(resolved.settings.clerk_dir)
        own_ids = arming_accounts(artifacts_root=root, live_state_root=self._live_root).get(account_id, set())
        if sid not in own_ids:
            raise LiveArmingRefused(
                LIVE_ARMING_INPUTS_CHANGED, "This bot does not belong to the selected live account."
            )
        now = self._clock()
        account = account_arming(
            live_account_id=account_id,
            artifacts_root=root,
            live_state_root=self._live_root,
            configured_envelope=configured_envelope(resolved.settings),
            now_ms=now,
            strategy_instance_ids=sorted(own_ids),
            custody_world="real_live" if primary_custody_world() == "real_live" else None,
        )
        current = account.statuses[sid]
        return ArmingStatusView(
            account_id=account_id,
            strategy_instance_id=sid,
            state=current.state,
            reason_code=current.reason_code,
            sessions_remaining=current.sessions_remaining,
            armed_instance_count=account.armed_instance_count,
            observed_at_ms=now,
        )

    def disarm(self, account_id: str, sid: str) -> ArmingStatusView:
        disarm(
            strategy_instance_id=sid, artifacts_root=self._artifacts_root, live_account_id=account_id, clock=self._clock
        )
        try:
            return self.status(account_id, sid)
        except (BrokerUnbound, BrokerConfigurationError, LiveArmingRefused, OSError, ValueError):
            logger.warning(
                "Disarmed bot; current account arming count is unavailable",
                exc_info=True,
                extra={"account_id": account_id, "strategy_instance_id": sid},
            )
            return ArmingStatusView(
                account_id=account_id,
                strategy_instance_id=sid,
                state="disarmed",
                reason_code="LIVE_ARMING_REVOKED",
                sessions_remaining=0,
                armed_instance_count=None,
                observed_at_ms=self._clock(),
            )

    @staticmethod
    def _require_account(expected: str, actual: str) -> None:
        if expected != actual:
            raise LiveArmingRefused(LIVE_ARMING_INPUTS_CHANGED, "The arming plan belongs to another account.")


def get_alpaca_live_arming_service() -> AlpacaLiveArmingService:
    return AlpacaLiveArmingService()
