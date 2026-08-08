"""Public orchestrator for observed synthetic Alpaca SQLite custody drills.

The fault scenarios cross the highest existing production hooks that can run
without network access. Unsupported source semantics remain typed ``PARTIAL``
limitations instead of being upgraded by a separate unit-test citation.
"""

from __future__ import annotations

from pathlib import Path

from app.broker.alpaca.fault_injection import get_fault_injection_registry
from app.services.account_custody_synthetic_scenarios import (
    SYNTHETIC_CUSTODY_SCENARIO_IDS,
)
from app.services.alpaca_sqlite_synthetic_drill_support import (
    FaultSeamLimitation,
    FaultSeamLimitationCode,
    SimulatedBrokerProof,
    SyntheticCustodyDrillReport,
    SyntheticDrillWorksheet,
    SyntheticScenarioObservation,
    SyntheticScenarioStatus,
)
from app.services.alpaca_sqlite_synthetic_fault_drills import lost_cancel, lost_submit
from app.services.alpaca_sqlite_synthetic_frame_drills import (
    cancel_fill_race,
    duplicate_redelivery,
    gap_reconcile,
)
from app.services.alpaca_sqlite_synthetic_state_drills import (
    bot_isolation,
    restart_in_flight,
    unknown_account_scope,
)


async def run_synthetic_custody_drills(
    artifacts_root: Path,
) -> SyntheticCustodyDrillReport:
    """Run the canonical custody matrix against throwaway SQLite authorities."""
    artifacts_root.mkdir(parents=True, exist_ok=True)
    observations = (
        await lost_submit(artifacts_root),
        await duplicate_redelivery(artifacts_root),
        await cancel_fill_race(artifacts_root),
        await lost_cancel(artifacts_root),
        await gap_reconcile(artifacts_root),
        await bot_isolation(artifacts_root),
        await unknown_account_scope(artifacts_root),
        await restart_in_flight(artifacts_root),
    )
    observed_scenario_ids = tuple(item.scenario_id for item in observations)
    if observed_scenario_ids != SYNTHETIC_CUSTODY_SCENARIO_IDS:
        raise RuntimeError("synthetic custody drills must follow canonical scenario order")
    registry_status = get_fault_injection_registry().status()
    return SyntheticCustodyDrillReport(
        account_prefix="QUAL1415-",
        live_scenario_executed=False,
        production_generation_opened_read_write=False,
        fault_injection_permitted=bool(registry_status["permitted"]),
        fault_seam_exercised=any(item.fault_seam_exercised for item in observations),
        fault_registry_write_armed=tuple(str(item["kind"]) for item in registry_status["write"]),
        fault_registry_frame_armed=tuple(str(item["kind"]) for item in registry_status["frame"]),
        observations=observations,
    )


__all__ = [
    "FaultSeamLimitation",
    "FaultSeamLimitationCode",
    "SimulatedBrokerProof",
    "SyntheticCustodyDrillReport",
    "SyntheticDrillWorksheet",
    "SyntheticScenarioObservation",
    "SyntheticScenarioStatus",
    "run_synthetic_custody_drills",
]
