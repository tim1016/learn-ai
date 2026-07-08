import type { LiveInstanceStatus } from '../../../../api/live-instances.types';

function normalizedSymbol(value: string | null | undefined): string {
  return value?.trim().toUpperCase() ?? '';
}

function singleLongStockActionSymbol(action: LiveInstanceStatus['action_plan']): string {
  if (!action || action.on_enter.length !== 1) return '';
  const [leg] = action.on_enter;
  if (leg.position !== 'long' || leg.instrument.kind !== 'stock') return '';
  return normalizedSymbol(leg.instrument.underlying);
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
      params['signal_stream'] = signalStream.trim().toUpperCase();
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
  return params;
}
