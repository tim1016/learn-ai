import type { LiveInstanceStatus } from '../../../../api/live-instances.types';

import { redeployQueryParamsForStatus } from './redeploy-query-params';

describe('redeployQueryParamsForStatus', () => {
  it('projects provenance and start defaults into deploy query params', () => {
    const status = {
      strategy_instance_id: 'DEPVAL-DIA-20260626',
      provenance: {
        strategy_spec_path: 'specs/dia.json',
        qc_audit_copy_path: 'audits/dia.json',
        qc_cloud_backtest_id: 'bt-123',
        account_id: 'DU123',
        run_id: 'parent-run',
        live_config: { symbol: 'dia' },
      },
      start_defaults: {
        strategy: 'deployment_validation',
      },
      symbol: 'dia',
      operator_surface: {
        current_risk: {
          posture: 'LONG',
          owned_positions: { DIA: 5 },
          pending_order_count: 2,
        },
      },
    } as unknown as LiveInstanceStatus;

    expect(redeployQueryParamsForStatus(status)).toEqual({
      spec_path: 'specs/dia.json',
      qc_audit_copy_path: 'audits/dia.json',
      qc_backtest_id: 'bt-123',
      signal_stream: 'DIA',
      parent_run_id: 'parent-run',
      instance_id: 'DEPVAL-DIA-20260626',
      inherited_symbol: 'DIA',
      inherited_symbol_source: 'run_ledger.live_config.symbol signal stream',
      strategy_key: 'deployment_validation',
      inherited_exposure_posture: 'LONG',
      inherited_exposure_positions: '{"DIA":5}',
      inherited_exposure_pending_order_count: '2',
      inherited_exposure_source: 'operator_surface.current_risk',
    });
  });

  it('labels inherited symbols sourced from the action plan trade target', () => {
    const status = {
      strategy_instance_id: 'DEPVAL-MU-20260701',
      provenance: {
        run_id: 'parent-run',
        live_config: { symbol: 'SPY' },
      },
      symbol: 'MU',
      action_plan: {
        on_enter: [
          {
            leg_id: 'leg_1',
            instrument: { kind: 'stock', underlying: 'MU' },
            position: 'long',
            qty_ratio: 1,
          },
        ],
        on_exit: [{ kind: 'close_leg', entry_leg_id: 'leg_1' }],
      },
      start_defaults: null,
    } as unknown as LiveInstanceStatus;

    expect(redeployQueryParamsForStatus(status)).toMatchObject({
      inherited_symbol: 'MU',
      inherited_symbol_source: 'run_ledger.live_config.action stock target',
      signal_stream: 'SPY',
    });
  });

  it('omits absent optional provenance fields', () => {
    const status = {
      strategy_instance_id: 'BOT',
      provenance: null,
      start_defaults: null,
    } as unknown as LiveInstanceStatus;

    expect(redeployQueryParamsForStatus(status)).toEqual({});
  });
});
