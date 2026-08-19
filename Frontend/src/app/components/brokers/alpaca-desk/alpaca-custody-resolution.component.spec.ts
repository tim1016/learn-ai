import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { CustodyDiagnosis } from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaCustodyResolutionComponent } from './alpaca-custody-resolution.component';
function diagnosis(overrides: Partial<CustodyDiagnosis> = {}): CustodyDiagnosis {
  return {
    broker: 'alpaca',
    account_id: 'PA1',
    authority_kind: 'sqlite',
    in_sync: true,
    observed_at_ms: 1,
    snapshot_version: 'v1',
    resolution_posture: 'paper',
    resolvable: false,
    blocked_reason: null,
    divergences: [],
    resolution_plan: [],
    ...overrides,
  };
}

function service(value: CustodyDiagnosis): Partial<BrokersService> {
  return { getCustodyDiagnosis: vi.fn().mockResolvedValue(value) };
}

describe('AlpacaCustodyResolutionComponent', () => {
  it('renders the SQLite custody comparison as read-only evidence', async () => {
    await render(AlpacaCustodyResolutionComponent, {
      providers: [{ provide: BrokersService, useValue: service(diagnosis()) }],
    });

    expect(await screen.findByText(/in sync/i)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /resolve/i })).toBeNull();
  });

  it('shows divergence evidence without offering a generic mutation', async () => {
    await render(AlpacaCustodyResolutionComponent, {
      providers: [{
        provide: BrokersService,
        useValue: service(diagnosis({
          authority_kind: 'sqlite',
          in_sync: false,
          resolvable: true,
          divergences: [{
            kind: 'needs_review',
            state: 'needs_review',
            explanation: 'An unresolved submission cannot be mapped to any broker outcome.',
            possible_causes: ['Broker evidence is incomplete.'],
            position_deltas: [],
            resolution_step: null,
            prerequisite_detail: null,
            evidence_refs: ['order-ref-abc123'],
          }],
        })),
      }],
    });

    expect(await screen.findByText(/cannot be mapped/i)).toBeTruthy();
    expect(screen.getByText('order-ref-abc123')).toBeTruthy();
    expect(screen.getByText(/evidence-bound recovery actions/i)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /resolve/i })).toBeNull();
  });
});
