import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { CustodyDiagnosis } from '../../../api/alpaca.types';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaCustodyResolutionComponent } from './alpaca-custody-resolution.component';
import { resourceTarget } from '../../../fleet/resource-target';

const TARGET = resourceTarget('alpaca', 'clrk_spec', { accountId: 'PA1', bindingGeneration: 1, routingEpoch: 1 });
function diagnosis(overrides: Partial<CustodyDiagnosis> = {}): CustodyDiagnosis {
  return {
    broker: 'alpaca',
    account_id: 'PA1',
    authority_kind: 'real_paper',
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
      inputs: { target: TARGET },
      providers: [{ provide: BrokersService, useValue: service(diagnosis()) }],
    });

    expect(await screen.findByLabelText("The Clerk's records and Alpaca agree")).toBeTruthy();
    expect(screen.queryByRole('button', { name: /resolve/i })).toBeNull();
  });

  it('shows divergence evidence without offering a generic mutation', async () => {
    await render(
      `<app-alpaca-custody-resolution [target]="target">
        <p>Recovery is on this host page.</p>
      </app-alpaca-custody-resolution>`,
      {
        imports: [AlpacaCustodyResolutionComponent],
        componentProperties: { target: TARGET },
        providers: [{
          provide: BrokersService,
          useValue: service(diagnosis({
            authority_kind: 'real_paper',
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
      },
    );

    expect(await screen.findByText(/cannot be mapped/i)).toBeTruthy();
    expect(screen.getByRole('heading', { name: "The Clerk's records and Alpaca disagree" })).toBeTruthy();
    expect(screen.getByText('order-ref-abc123')).toBeTruthy();
    // The host names where its recovery actions are; the card never guesses.
    expect(screen.getByText('Recovery is on this host page.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /resolve/i })).toBeNull();
  });
});
