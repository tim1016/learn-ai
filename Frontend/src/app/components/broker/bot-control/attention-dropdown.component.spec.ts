import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { TraderPrimaryRemediation } from '../../../api/live-instances.types';
import { makeOperatorSurfaceFixture } from '../../../testing/operator-surface-fixtures';
import { AttentionDropdownComponent } from './attention-dropdown.component';

describe('AttentionDropdownComponent', () => {
  it('renders backend-authored attention copy and emits the row remediation', async () => {
    const surface = makeOperatorSurfaceFixture();
    const remediation: TraderPrimaryRemediation = {
      kind: 'invoke_endpoint',
      endpoint: 'reconcile_instance',
      method: 'POST',
      path_template: '/api/live-instances/{strategy_instance_id}/reconcile',
    };
    let captured: TraderPrimaryRemediation | null = null;

    await render(AttentionDropdownComponent, {
      inputs: {
        guidance: surface.trader_guidance,
        groups: [
          {
            code: 'reconciliation',
            severity: 'warning',
            headline: 'Reconciliation is not fresh-clean',
            explanation: 'Reconciliation state is NOT_AVAILABLE.',
            operator_next_step: 'Run reconciliation and wait for a clean or adopted receipt.',
            remediation,
          },
        ],
        open: true,
      },
      on: {
        remediationSelected: (action: TraderPrimaryRemediation) => {
          captured = action;
        },
      },
    });

    expect(screen.getByText('Reconciliation is not fresh-clean')).toBeTruthy();
    expect(screen.getByText('Reconciliation state is NOT_AVAILABLE.')).toBeTruthy();
    await screen.getByRole('button', { name: /reconcile now for reconciliation is not fresh-clean/i }).click();
    expect(captured).toEqual(remediation);
  });

  it('does not render an action for no-op remediations', async () => {
    const surface = makeOperatorSurfaceFixture();

    await render(AttentionDropdownComponent, {
      inputs: {
        guidance: surface.trader_guidance,
        groups: [
          {
            code: 'host_process',
            severity: 'info',
            headline: 'No live runtime is bound',
            explanation: 'Host process is EXITED; live-only commands cannot execute until a bot process is started.',
            operator_next_step: 'Use this as context for the blocked broker/reconciliation proofs, not as a separate broker problem.',
            remediation: { kind: 'none', reason: 'MONITOR_ONLY' },
          },
        ],
        open: true,
      },
    });

    expect(screen.getByText('No live runtime is bound')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /for bot process is not running/i })).toBeNull();
  });
});
