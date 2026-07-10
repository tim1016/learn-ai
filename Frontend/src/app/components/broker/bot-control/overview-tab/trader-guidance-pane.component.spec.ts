import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type {
  OperatorSurface,
} from '../../../../api/live-instances.types';
import { makeOperatorSurfaceFixture } from '../../../../testing/operator-surface-fixtures';
import { TraderGuidancePaneComponent } from './trader-guidance-pane.component';

function makeSurface(overrides: Partial<OperatorSurface> = {}): OperatorSurface {
  return makeOperatorSurfaceFixture(overrides);
}

describe('TraderGuidancePaneComponent', () => {
  it('renders backend-authored trader guidance with friendly technical diagnostics', async () => {
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
            proof_lines: [
              {
                id: 'broker-proof',
                label: 'broker.connection',
                message: 'Broker proof is not available yet.',
                detail: 'Account safety proof is not recorded. Broker connection has not been proven.',
                tone: 'attention',
              },
            ],
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
    expect(screen.getByText('Proof stack')).toBeTruthy();
    expect(screen.getByText('Broker Connection')).toBeTruthy();
    expect(screen.getByText('Broker proof is not available yet.')).toBeTruthy();
    const diagnostics = screen.getByTestId('trader-guidance-advanced-evidence');
    expect(diagnostics.textContent).toContain('Technical diagnostics');
    expect(diagnostics.textContent).toContain('Reconciliation is not available.');
    expect(diagnostics.textContent).not.toContain('reconciliation.state');
    expect(diagnostics.textContent).not.toContain('NOT_AVAILABLE');
    expect(diagnostics.querySelector('.evidence-row')?.getAttribute('title'))
      .toContain('Source: Reconcile Receipt. Gate: Latest Reconcile');
  });

  it('renders as documentation without interactive controls', async () => {
    await render(TraderGuidancePaneComponent, {
      inputs: { surface: makeOperatorSurfaceFixture() },
    });

    const pane = screen.getByTestId('trader-guidance-pane');
    expect(pane.querySelector('button')).toBeNull();
    expect(pane.querySelector('a')).toBeNull();
    expect(screen.queryByTestId('trader-guidance-timeline')).toBeNull();
  });
});
