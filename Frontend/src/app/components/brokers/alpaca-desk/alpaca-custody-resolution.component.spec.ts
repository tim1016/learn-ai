import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { CustodyDiagnosis } from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaCustodyResolutionComponent } from './alpaca-custody-resolution.component';

function diagnosis(overrides: Partial<CustodyDiagnosis> = {}): CustodyDiagnosis {
  return {
    broker: 'alpaca', account_id: 'PA1', in_sync: true, observed_at_ms: 1,
    snapshot_version: 'v1', resolution_posture: 'paper', resolvable: false,
    blocked_reason: null, divergences: [], resolution_plan: [], ...overrides,
  } as CustodyDiagnosis;
}

function svc(d: CustodyDiagnosis) {
  return { getCustodyDiagnosis: vi.fn().mockResolvedValue(d) };
}

describe('AlpacaCustodyResolutionComponent', () => {
  it('shows the in-sync strip when clerk and broker agree', async () => {
    await render(AlpacaCustodyResolutionComponent, {
      providers: [{ provide: BrokersService, useValue: svc(diagnosis()) }],
    });
    expect(await screen.findByText(/in sync/i)).toBeTruthy();
  });

  it('shows the delta and explanation when diverged', async () => {
    const diverged = diagnosis({
      in_sync: false, resolvable: true,
      divergences: [{
        kind: 'exposure_attribution_mismatch', state: 'resolvable_now',
        explanation: 'The broker holds exposure the Clerk cannot map.',
        possible_causes: ['A bot process was terminated mid-run.'],
        position_deltas: [{ symbol: 'SPY', clerk_attributed_qty: 2, broker_observed_qty: 1 }],
        resolution_step: 'record_inventory_baseline', prerequisite_detail: null, evidence_refs: [],
      }],
      resolution_plan: [{ action_id: 'record_inventory_baseline', scope: 'account', mutates: true }],
    });
    await render(AlpacaCustodyResolutionComponent, {
      providers: [{ provide: BrokersService, useValue: svc(diverged) }],
    });
    expect(await screen.findByText(/cannot map/i)).toBeTruthy();
    expect(screen.getByText('SPY')).toBeTruthy();
    expect(screen.getByRole('button', { name: /resolve & sync/i })).toBeTruthy();
  });
});
