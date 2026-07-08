import type { ParamMap } from '@angular/router';

import type { ActionPlan } from '../../../api/action-plan.types';
import type { LiveInstanceStatus } from '../../../api/live-instances.types';
import type { ExposureCoherencePosture } from '../../../api/live-runs.types';

export interface DeployPrefillParams {
  strategyKey: string;
  specPath: string;
  qcBacktestId: string;
  qcAuditCopyPath: string;
  instanceId: string;
  inheritedSymbol: string;
  inheritedSymbolSource: string;
  inheritedExposurePosture: ExposureCoherencePosture | '';
  inheritedExposurePendingOrderCount: number | null;
  inheritedExposurePositions: Record<string, number>;
  inheritedExposureSource: string;
  parentRunId: string | null;
  signalStream: string;
}

const NON_NEGATIVE_INTEGER_RE = /^\d+$/;

export function normalizedSymbol(value: string | null | undefined): string {
  return value?.trim().toUpperCase() ?? '';
}

export function singleLongStockActionSymbol(action: ActionPlan | null | undefined): string {
  if (!action || action.on_enter.length !== 1) return '';
  const [leg] = action.on_enter;
  if (leg.position !== 'long' || leg.instrument.kind !== 'stock') return '';
  return normalizedSymbol(leg.instrument.underlying);
}

export function isExposurePosture(value: string | null): value is ExposureCoherencePosture {
  return (
    value === 'FLAT' ||
    value === 'LONG' ||
    value === 'SHORT' ||
    value === 'MIXED' ||
    value === 'UNKNOWN'
  );
}

export function parseExposurePositions(value: string | null): Record<string, number> | null {
  if (value === null) return null;
  try {
    const parsed: unknown = JSON.parse(value);
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
    const out: Record<string, number> = {};
    for (const [symbol, rawQuantity] of Object.entries(parsed)) {
      const normalized = symbol.trim().toUpperCase();
      if (!normalized || typeof rawQuantity !== 'number' || !Number.isInteger(rawQuantity)) {
        return null;
      }
      if (rawQuantity !== 0) {
        out[normalized] = rawQuantity;
      }
    }
    return Object.fromEntries(Object.entries(out).sort(([left], [right]) => left.localeCompare(right)));
  } catch {
    return null;
  }
}

export function exposurePositionsLabel(positions: Record<string, number>): string {
  const entries = Object.entries(positions).filter(([, quantity]) => quantity !== 0);
  if (!entries.length) return 'Flat';
  return entries
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([symbol, quantity]) => `${symbol} ${quantity}`)
    .join(', ');
}

function inheritedSymbolSource(status: LiveInstanceStatus, symbol: string): string {
  if (singleLongStockActionSymbol(status.action_plan) === symbol) {
    return 'run_ledger.live_config.action stock target';
  }
  const signalStream = status.provenance?.live_config?.['symbol'];
  if (typeof signalStream === 'string' && normalizedSymbol(signalStream) === symbol) {
    return 'run_ledger.live_config.symbol signal stream';
  }
  return 'strategy_spec.symbols fallback';
}

export function redeployQueryParamsForStatus(
  status: LiveInstanceStatus,
): Record<string, string> {
  const params: Record<string, string> = {};
  if (status.provenance) {
    if (status.provenance.strategy_spec_path) {
      params['spec_path'] = status.provenance.strategy_spec_path;
    }
    if (status.provenance.qc_audit_copy_path) {
      params['qc_audit_copy_path'] = status.provenance.qc_audit_copy_path;
    }
    if (status.provenance.qc_cloud_backtest_id) {
      params['qc_backtest_id'] = status.provenance.qc_cloud_backtest_id;
    }
    const signalStream = status.provenance.live_config?.['symbol'];
    if (typeof signalStream === 'string' && signalStream.trim() !== '') {
      params['signal_stream'] = normalizedSymbol(signalStream);
    }
    params['parent_run_id'] = status.provenance.run_id;
    params['instance_id'] = status.strategy_instance_id;
  }
  const inheritedSymbol = normalizedSymbol(status.symbol);
  if (inheritedSymbol) {
    params['inherited_symbol'] = inheritedSymbol;
    params['inherited_symbol_source'] = inheritedSymbolSource(status, inheritedSymbol);
  }
  if (status.start_defaults?.strategy) {
    params['strategy_key'] = status.start_defaults.strategy;
  }
  const currentRisk = status.operator_surface?.current_risk;
  if (currentRisk) {
    params['inherited_exposure_posture'] = currentRisk.posture;
    params['inherited_exposure_positions'] = JSON.stringify(currentRisk.owned_positions ?? {});
    if (typeof currentRisk.pending_order_count === 'number') {
      params['inherited_exposure_pending_order_count'] = String(currentRisk.pending_order_count);
    }
    params['inherited_exposure_source'] = 'operator_surface.current_risk';
  }
  return params;
}

export function deployPrefillParamsFromQuery(queryParamMap: ParamMap): DeployPrefillParams {
  const seedExposurePosture = queryParamMap.get('inherited_exposure_posture');
  const seedExposurePending = queryParamMap.get('inherited_exposure_pending_order_count');
  const parsedPositions = parseExposurePositions(queryParamMap.get('inherited_exposure_positions'));
  return {
    strategyKey: queryParamMap.get('strategy_key') ?? '',
    specPath: queryParamMap.get('spec_path') ?? '',
    qcBacktestId: queryParamMap.get('qc_backtest_id') ?? '',
    qcAuditCopyPath: queryParamMap.get('qc_audit_copy_path') ?? '',
    instanceId: queryParamMap.get('instance_id') ?? '',
    inheritedSymbol: normalizedSymbol(queryParamMap.get('inherited_symbol')),
    inheritedSymbolSource: queryParamMap.get('inherited_symbol_source')?.trim() ?? '',
    inheritedExposurePosture: isExposurePosture(seedExposurePosture) ? seedExposurePosture : '',
    inheritedExposurePendingOrderCount:
      seedExposurePending !== null && NON_NEGATIVE_INTEGER_RE.test(seedExposurePending)
        ? Number.parseInt(seedExposurePending, 10)
        : null,
    inheritedExposurePositions: parsedPositions ?? {},
    inheritedExposureSource: queryParamMap.get('inherited_exposure_source')?.trim() ?? '',
    parentRunId: queryParamMap.get('parent_run_id') || null,
    signalStream: normalizedSymbol(queryParamMap.get('signal_stream')),
  };
}
