import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { OperatorSurface, TraderPrimaryRemediation } from '../../../../api/live-instances.types';
import { makeOperatorSurfaceFixture } from '../../../../testing/live-instance-status-fixtures';
import { TraderGuidancePaneComponent } from './trader-guidance-pane.component';

function makeSurface(overrides: Partial<OperatorSurface> = {}): OperatorSurface {
  return makeOperatorSurfaceFixture(overrides);
}

describe('TraderGuidancePaneComponent', () => {
  it('renders backend-authored trader guidance and advanced evidence verbatim', async () => {
    const base = makeOperatorSurfaceFixture();
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeSurface({
          trader_guidance: {
            ...base.trader_guidance,
            headline: 'Broker state is not proven enough to submit.',
            explanation: 'The backend cannot prove the broker/session/reconciliation facts needed before a submit.',
            risk_headline: 'Do not treat stale or missing broker evidence as live truth',
            risk_explanation: 'Reconnect or reconcile until the broker evidence is fresh and explicit.',
            advanced_evidence: [
              {
                label: 'reconciliation.state',
                value: 'NOT_AVAILABLE',
                source: 'reconcile_receipt',
                gate_id: 'latest_reconcile',
                ts_ms: null,
                ts_ms_resolved: false,
              },
            ],
          },
        }),
      },
    });

    expect(screen.getByText('Broker state is not proven enough to submit.')).toBeTruthy();
    expect(screen.getByText('The backend cannot prove the broker/session/reconciliation facts needed before a submit.')).toBeTruthy();
    expect(screen.getByText('Do not treat stale or missing broker evidence as live truth')).toBeTruthy();
    expect(screen.getByText('Reconnect or reconcile until the broker evidence is fresh and explicit.')).toBeTruthy();
    expect(screen.getByTestId('trader-guidance-advanced-evidence').textContent)
      .toContain('reconciliation.state');
    expect(screen.getByTestId('trader-guidance-advanced-evidence').textContent)
      .toContain('NOT_AVAILABLE');
  });

  it('emits the backend remediation object for invoke_endpoint actions', async () => {
    const remediation: TraderPrimaryRemediation = {
      kind: 'invoke_endpoint',
      endpoint: 'reconcile_instance',
      method: 'POST',
      path_template: '/api/live-instances/{strategy_instance_id}/reconcile',
    };
    const base = makeOperatorSurfaceFixture();
    let captured: TraderPrimaryRemediation | null = null;
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeSurface({
          submit_readiness: {
            ...base.submit_readiness,
            code: 'broker_state_unproven',
            label: 'Broker state unproven',
            can_submit: false,
            blocking_reason_codes: ['RECONCILIATION_NOT_AVAILABLE'],
          },
          trader_guidance: {
            ...base.trader_guidance,
            situation_code: 'broker_state_unproven',
            primary_remediation: remediation,
          },
        }),
      },
      on: {
        primaryRemediationSelected: (action: TraderPrimaryRemediation) => {
          captured = action;
        },
      },
    });

    await screen.getByRole('button', { name: /reconcile now/i }).click();
    expect(captured).toEqual(remediation);
  });

  it('does not invent a primary action for none remediations', async () => {
    await render(TraderGuidancePaneComponent, {
      inputs: { surface: makeOperatorSurfaceFixture() },
    });

    expect(screen.queryByTestId('trader-guidance-primary-remediation')).toBeNull();
  });
});
