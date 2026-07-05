import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type {
  StrategyValidationCatalog,
  StrategyValidationDetail,
} from '../../services/strategy-validation.types';
import { StrategyValidationService } from '../../services/strategy-validation.service';
import { StrategyValidationComponent } from './strategy-validation.component';

const DEPLOYMENT_DETAIL: StrategyValidationDetail = {
  strategy_key: 'deployment_validation',
  display_name: 'Deployment Validation',
  description: 'Two-green-minute deployment validation primitive.',
  validation_state: 'validated',
  deployable: true,
  settings_file_ref: 'PythonDataService/app/engine/strategy/spec/fixtures/deployment_validation.spec.json',
  settings_file_sha256: 'spec-sha',
  qc_cloud_backtest_id: 'd2fe45a7142e88575f6fbd75229f8681',
  audit_copy_ref: 'references/qc-shadow/DeploymentValidationAlgorithm.py',
  audit_copy_sha256: 'audit-sha',
  reconciliation_ref: 'references/qc-shadow/backtests/2024-03-28_to_2026-03-03/attribution.md',
  validation_case_symbol: 'SPY',
  reconciliation_status: 'passed',
  diagnostics: {
    verdict: 'passed',
    trades_matched: 56,
    trades_validated: 56,
    pnl_max_abs_diff: '0.00',
    divergence_counts: { fill_price_drift: 2 },
    notes: ['QC receipt stored.'],
  },
  reference_code: {
    path: 'references/qc-shadow/DeploymentValidationAlgorithm.py',
    sha256: 'audit-sha',
    language: 'python',
    source: 'class DeploymentValidationAlgorithm(QCAlgorithm):\n    pass\n',
  },
};

const ORB_DETAIL: StrategyValidationDetail = {
  strategy_key: 'spy_orb',
  display_name: 'Opening Range Breakout',
  description: 'Opening range breakout strategy.',
  validation_state: 'needs_validation',
  deployable: false,
  settings_file_ref: null,
  settings_file_sha256: null,
  qc_cloud_backtest_id: null,
  audit_copy_ref: null,
  audit_copy_sha256: null,
  reconciliation_ref: null,
  validation_case_symbol: null,
  reconciliation_status: null,
  diagnostics: null,
  reference_code: null,
};

const CATALOG: StrategyValidationCatalog = {
  strategies: [
    DEPLOYMENT_DETAIL,
    ORB_DETAIL,
  ],
};

class FakeStrategyValidationService {
  getCatalog = vi.fn().mockResolvedValue(CATALOG);
  getDetail = vi.fn((key: string) => Promise.resolve(key === 'deployment_validation' ? DEPLOYMENT_DETAIL : ORB_DETAIL));
}

describe('StrategyValidationComponent', () => {
  it('renders validated and unvalidated strategies in the catalog', async () => {
    await render(StrategyValidationComponent, {
      providers: [{ provide: StrategyValidationService, useClass: FakeStrategyValidationService }],
    });

    expect(await screen.findByRole('heading', { name: 'Strategy Validation' })).toBeTruthy();
    expect(await screen.findByRole('button', { name: /Deployment Validation/ })).toBeTruthy();
    expect(screen.getByRole('button', { name: /Opening Range Breakout/ })).toBeTruthy();
    expect(screen.getAllByText('Validated').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Needs validation').length).toBeGreaterThan(0);
  });

  it('opens detail with QC evidence and reference code without rendering internal port source', async () => {
    await render(StrategyValidationComponent, {
      providers: [{ provide: StrategyValidationService, useClass: FakeStrategyValidationService }],
    });

    expect(await screen.findByText('d2fe45a7142e88575f6fbd75229f8681')).toBeTruthy();
    expect(screen.getByText('SPY')).toBeTruthy();
    expect(screen.getByText('56 trades matched')).toBeTruthy();
    expect(screen.getByText('56 trades validated')).toBeTruthy();
    expect(screen.getByText('Fill Price Drift')).toBeTruthy();
    expect(screen.queryByText('fill_price_drift')).toBeNull();
    expect(screen.getByText(/class DeploymentValidationAlgorithm/)).toBeTruthy();
    expect(screen.queryByText(/DeploymentValidationConsecutiveGreen/)).toBeNull();
  });

  it('switches to the selected strategy detail', async () => {
    await render(StrategyValidationComponent, {
      providers: [{ provide: StrategyValidationService, useClass: FakeStrategyValidationService }],
    });

    fireEvent.click(await screen.findByRole('button', { name: /Opening Range Breakout/ }));

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Opening Range Breakout' })).toBeTruthy();
    });
    expect(screen.getAllByText('Needs validation').length).toBeGreaterThan(0);
    expect(screen.getByText('Validation evidence has not been registered yet.')).toBeTruthy();
  });
});
