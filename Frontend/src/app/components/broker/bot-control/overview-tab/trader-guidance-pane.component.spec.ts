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

  it('renders the account owner section when account_owner is present', async () => {
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeSurface({
          account_owner: {
            account_id: 'DU999',
            generation: 7,
            phase: 'accepting',
            recorded_at_ms: 1_800_000_000_000,
            source: 'account_owner',
          },
        }),
      },
    });

    expect(screen.getByText('Account owner')).toBeTruthy();
    expect(screen.getByText('DU999')).toBeTruthy();
    expect(screen.getByText('accepting')).toBeTruthy();
    expect(screen.getByText('7')).toBeTruthy();
  });

  it('hides the account owner section when account_owner is null', async () => {
    await render(TraderGuidancePaneComponent, {
      inputs: { surface: makeSurface({ account_owner: null }) },
    });

    expect(screen.queryByText('Account owner')).toBeNull();
    expect(screen.queryByText('Generation')).toBeNull();
  });

  it('omits the Generation row when account_owner.generation is null', async () => {
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeSurface({
          account_owner: {
            account_id: 'DU888',
            generation: null,
            phase: 'reconnecting',
            recorded_at_ms: null,
            source: null,
          },
        }),
      },
    });

    expect(screen.getByText('DU888')).toBeTruthy();
    expect(screen.queryByText('Generation')).toBeNull();
  });

  it('renders blocking_reason_codes as a list when present', async () => {
    const base = makeOperatorSurfaceFixture();
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeSurface({
          submit_readiness: {
            ...base.submit_readiness,
            blocking_reason_codes: ['BROKER_CONNECTION_UNKNOWN', 'RECONCILIATION_NOT_AVAILABLE'],
          },
        }),
      },
    });

    const list = screen.getByRole('list', { name: /blocking reason codes/i });
    expect(list.textContent).toContain('BROKER_CONNECTION_UNKNOWN');
    expect(list.textContent).toContain('RECONCILIATION_NOT_AVAILABLE');
  });

  it('hides the blocking reason codes list when empty', async () => {
    const base = makeOperatorSurfaceFixture();
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeSurface({
          submit_readiness: {
            ...base.submit_readiness,
            blocking_reason_codes: [],
          },
        }),
      },
    });

    expect(screen.queryByRole('list', { name: /blocking reason codes/i })).toBeNull();
  });

  it('renders attention groups with their headline and explanation', async () => {
    const base = makeOperatorSurfaceFixture();
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeSurface({
          trader_guidance: {
            ...base.trader_guidance,
            additional_attention_groups: [
              {
                code: 'broker_connection',
                severity: 'warning',
                headline: 'Broker session is not connected',
                explanation: 'The broker connection evidence is UNKNOWN.',
              },
              {
                code: 'reconciliation',
                severity: 'critical',
                headline: 'Reconciliation failed',
                explanation: 'No reconciliation receipt available.',
              },
            ],
          },
        }),
      },
    });

    expect(screen.getByText('Broker session is not connected')).toBeTruthy();
    expect(screen.getByText('The broker connection evidence is UNKNOWN.')).toBeTruthy();
    expect(screen.getByText('Reconciliation failed')).toBeTruthy();
    expect(screen.getByText('No reconciliation receipt available.')).toBeTruthy();
  });

  it('hides attention section when there are no attention groups', async () => {
    const base = makeOperatorSurfaceFixture();
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeSurface({
          trader_guidance: {
            ...base.trader_guidance,
            additional_attention_groups: [],
          },
        }),
      },
    });

    expect(screen.queryByText('Attention')).toBeNull();
  });

  it('renders the submit readiness label and explanation', async () => {
    const base = makeOperatorSurfaceFixture();
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeSurface({
          submit_readiness: {
            ...base.submit_readiness,
            label: 'Safe to submit',
            explanation: 'All gates passed.',
            can_submit: true,
            blocking_reason_codes: [],
          },
        }),
      },
    });

    expect(screen.getByText('Safe to submit')).toBeTruthy();
    expect(screen.getByText('All gates passed.')).toBeTruthy();
  });

  it('renders the risk section from trader_guidance', async () => {
    const base = makeOperatorSurfaceFixture();
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeSurface({
          trader_guidance: {
            ...base.trader_guidance,
            risk_headline: 'Position risk is elevated',
            risk_explanation: 'Monitor open positions carefully.',
          },
        }),
      },
    });

    expect(screen.getByText('Position risk is elevated')).toBeTruthy();
    expect(screen.getByText('Monitor open positions carefully.')).toBeTruthy();
  });

  it('renders advanced evidence source as small text when present', async () => {
    const base = makeOperatorSurfaceFixture();
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeSurface({
          trader_guidance: {
            ...base.trader_guidance,
            advanced_evidence: [
              {
                label: 'broker.connection',
                value: 'DISCONNECTED',
                source: 'operator_surface',
                gate_id: null,
                ts_ms: null,
                ts_ms_resolved: false,
              },
            ],
          },
        }),
      },
    });

    const evidenceEl = screen.getByTestId('trader-guidance-advanced-evidence');
    expect(evidenceEl.textContent).toContain('broker.connection');
    expect(evidenceEl.textContent).toContain('DISCONNECTED');
    expect(evidenceEl.textContent).toContain('operator_surface');
    const small = evidenceEl.querySelector('small');
    expect(small?.textContent).toBe('operator_surface');
  });

  it('omits the source small element when fact.source is null', async () => {
    const base = makeOperatorSurfaceFixture();
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeSurface({
          trader_guidance: {
            ...base.trader_guidance,
            advanced_evidence: [
              {
                label: 'some.metric',
                value: 'UNKNOWN',
                source: null,
                gate_id: null,
                ts_ms: null,
                ts_ms_resolved: false,
              },
            ],
          },
        }),
      },
    });

    const evidenceEl = screen.getByTestId('trader-guidance-advanced-evidence');
    expect(evidenceEl.querySelector('small')).toBeNull();
  });

  it('applies data-situation and data-submit-readiness attributes from the surface', async () => {
    const base = makeOperatorSurfaceFixture();
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeSurface({
          submit_readiness: {
            ...base.submit_readiness,
            code: 'account_frozen',
          },
          trader_guidance: {
            ...base.trader_guidance,
            situation_code: 'account_frozen',
          },
        }),
      },
    });

    const pane = screen.getByTestId('trader-guidance-pane');
    expect(pane.getAttribute('data-situation')).toBe('account_frozen');
    expect(pane.getAttribute('data-submit-readiness')).toBe('account_frozen');
  });
});
