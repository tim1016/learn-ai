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

  it('renders the account_owner section when account_owner is present', async () => {
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

    const pane = screen.getByTestId('trader-guidance-pane');
    expect(pane.textContent).toContain('DU999');
    expect(pane.textContent).toContain('accepting');
    expect(pane.textContent).toContain('7');
  });

  it('hides the account_owner section when account_owner is null', async () => {
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeSurface({ account_owner: null }),
      },
    });

    const pane = screen.getByTestId('trader-guidance-pane');
    expect(pane.textContent).not.toContain('Account owner');
    expect(pane.querySelector?.('[class*="owner-band"]')).toBeNull();
  });

  it('sets the data-situation and data-submit-readiness attributes on the pane', async () => {
    const surface = makeSurface({
      submit_readiness: {
        ...makeOperatorSurfaceFixture().submit_readiness,
        code: 'account_frozen',
        label: 'Account frozen',
        can_submit: false,
        blocking_reason_codes: ['ACCOUNT_FROZEN'],
      },
      trader_guidance: {
        ...makeOperatorSurfaceFixture().trader_guidance,
        situation_code: 'account_frozen',
      },
    });
    await render(TraderGuidancePaneComponent, { inputs: { surface } });

    const pane = screen.getByTestId('trader-guidance-pane');
    expect(pane.getAttribute('data-situation')).toBe('account_frozen');
    expect(pane.getAttribute('data-submit-readiness')).toBe('account_frozen');
  });

  it('applies the can-submit class to the readiness band when can_submit is true', async () => {
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeSurface({
          submit_readiness: {
            ...makeOperatorSurfaceFixture().submit_readiness,
            code: 'safe_to_submit',
            can_submit: true,
            blocking_reason_codes: [],
          },
        }),
      },
    });

    const readinessBand = (screen.getByTestId('trader-guidance-pane') as HTMLElement)
      .querySelector('.readiness-band');
    expect(readinessBand?.classList.contains('can-submit')).toBe(true);
  });

  it('does not apply can-submit class when can_submit is false', async () => {
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeSurface({
          submit_readiness: {
            ...makeOperatorSurfaceFixture().submit_readiness,
            code: 'broker_state_unproven',
            can_submit: false,
            blocking_reason_codes: ['BROKER_CONNECTION_DISCONNECTED'],
          },
        }),
      },
    });

    const readinessBand = (screen.getByTestId('trader-guidance-pane') as HTMLElement)
      .querySelector('.readiness-band');
    expect(readinessBand?.classList.contains('can-submit')).toBe(false);
  });

  it('renders blocking reason codes as list items in the readiness band', async () => {
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeSurface({
          submit_readiness: {
            ...makeOperatorSurfaceFixture().submit_readiness,
            code: 'blocked_before_submit',
            can_submit: false,
            blocking_reason_codes: ['BROKER_SAFETY_UNKNOWN', 'RECONCILIATION_NOT_AVAILABLE'],
          },
        }),
      },
    });

    const reasonList = screen.getByRole('list', { name: /blocking reason codes/i });
    expect(reasonList.textContent).toContain('BROKER_SAFETY_UNKNOWN');
    expect(reasonList.textContent).toContain('RECONCILIATION_NOT_AVAILABLE');
  });

  it('does not render the reason list when blocking_reason_codes is empty', async () => {
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeSurface({
          submit_readiness: {
            ...makeOperatorSurfaceFixture().submit_readiness,
            blocking_reason_codes: [],
          },
        }),
      },
    });

    expect(screen.queryByRole('list', { name: /blocking reason codes/i })).toBeNull();
  });

  it('renders multiple attention groups each with the correct severity class', async () => {
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
                headline: 'Broker session is disconnected',
                explanation: 'The broker connection evidence is not connected.',
              },
              {
                code: 'account_state',
                severity: 'critical',
                headline: 'Account is in a critical state',
                explanation: 'The account has a critical issue.',
              },
              {
                code: 'host_info',
                severity: 'info',
                headline: 'Host process notice',
                explanation: 'Minor host process information.',
              },
            ],
          },
        }),
      },
    });

    const pane = screen.getByTestId('trader-guidance-pane') as HTMLElement;
    const warningItem = pane.querySelector('.severity-warning');
    const criticalItem = pane.querySelector('.severity-critical');
    const infoItem = pane.querySelector('.severity-info');
    expect(warningItem?.textContent).toContain('Broker session is disconnected');
    expect(criticalItem?.textContent).toContain('Account is in a critical state');
    expect(infoItem?.textContent).toContain('Host process notice');
  });

  it('does not render the attention section when additional_attention_groups is empty', async () => {
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

    const pane = screen.getByTestId('trader-guidance-pane') as HTMLElement;
    expect(pane.querySelector('.attention-band')).toBeNull();
  });

  it('emits the invoke_capability remediation object when a capability action is clicked', async () => {
    const remediation: TraderPrimaryRemediation = {
      kind: 'invoke_capability',
      capability: 'resume',
    };
    const base = makeOperatorSurfaceFixture();
    let captured: TraderPrimaryRemediation | null = null;
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeSurface({
          trader_guidance: {
            ...base.trader_guidance,
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

    const button = screen.getByTestId('trader-guidance-primary-remediation') as HTMLButtonElement;
    expect(button.textContent).toContain('Resume');
    button.click();
    expect(captured).toEqual(remediation);
  });

  it('renders advanced evidence source as supplementary text', async () => {
    const base = makeOperatorSurfaceFixture();
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeSurface({
          trader_guidance: {
            ...base.trader_guidance,
            advanced_evidence: [
              {
                label: 'reconciliation.state',
                value: 'CLEAN',
                source: 'reconcile_receipt',
                gate_id: null,
                ts_ms: null,
                ts_ms_resolved: false,
              },
            ],
          },
        }),
      },
    });

    const evidenceSection = screen.getByTestId('trader-guidance-advanced-evidence');
    expect(evidenceSection.textContent).toContain('reconciliation.state');
    expect(evidenceSection.textContent).toContain('CLEAN');
    expect(evidenceSection.textContent).toContain('reconcile_receipt');
  });

  it('renders the risk headline and explanation from the backend', async () => {
    const base = makeOperatorSurfaceFixture();
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeSurface({
          trader_guidance: {
            ...base.trader_guidance,
            risk_headline: 'Unique risk headline for test',
            risk_explanation: 'Unique risk explanation for test.',
          },
        }),
      },
    });

    const pane = screen.getByTestId('trader-guidance-pane');
    expect(pane.textContent).toContain('Unique risk headline for test');
    expect(pane.textContent).toContain('Unique risk explanation for test.');
  });

  it('renders the submit readiness label and explanation', async () => {
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeSurface({
          submit_readiness: {
            ...makeOperatorSurfaceFixture().submit_readiness,
            label: 'Unique readiness label',
            explanation: 'Unique readiness explanation.',
            can_submit: false,
            blocking_reason_codes: [],
          },
        }),
      },
    });

    const pane = screen.getByTestId('trader-guidance-pane');
    expect(pane.textContent).toContain('Unique readiness label');
    expect(pane.textContent).toContain('Unique readiness explanation.');
  });
});
