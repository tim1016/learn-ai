import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type {
  LifecycleProjectionEventRow,
  OperatorSurface,
  TraderPrimaryRemediation,
} from '../../../../api/live-instances.types';
import { makeOperatorSurfaceFixture } from '../../../../testing/operator-surface-fixtures';
import { TraderGuidancePaneComponent } from './trader-guidance-pane.component';

function makeSurface(overrides: Partial<OperatorSurface> = {}): OperatorSurface {
  return makeOperatorSurfaceFixture(overrides);
}

function makeTimelineRow(): LifecycleProjectionEventRow {
  return {
    id: 12,
    account_id: 'DU123',
    strategy_instance_id: 'sid-x',
    run_id: 'run-x',
    event_id: 'intent_wal:run-x:1:ACK_FAILED_UNCERTAIN',
    event_type: 'BrokerOrderUncertain',
    category: 'order',
    node_id: 'ack_or_reconcile',
    gate_id: null,
    status: 'blocked',
    severity: 'warning',
    ts_ms: 1_700_000_001_000,
    ts_ms_resolved: true,
    source_artifact: 'intent_events.jsonl',
    source_type: 'broker_ack',
    source_seq: 1,
    source_offset: null,
    source_hash: null,
    summary: 'Broker acknowledgement failed; submit outcome is uncertain.',
    why: 'Probe broker before retrying this intent.',
    operator_next_step: 'PROBE_BROKER_BEFORE_RETRY',
    receipt_payload: { intent_id: 'intent-1' },
    evidence_refs: [],
    rendered_headline: null,
    rendered_template_id: null,
    inserted_at_ms: 1_700_000_001_100,
    updated_at_ms: 1_700_000_001_100,
  };
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

  it('renders backend-authored lifecycle timeline rows without deriving a verdict', async () => {
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeOperatorSurfaceFixture(),
        timelineRows: [makeTimelineRow()],
        timelineProjectionAvailable: true,
        timelineCanonicalFallbackRequired: false,
      },
    });

    const timeline = screen.getByTestId('trader-guidance-timeline');
    expect(timeline.textContent).toContain('What changed recently');
    expect(timeline.textContent).toContain('Broker acknowledgement failed; submit outcome is uncertain.');
    expect(timeline.textContent).toContain('Probe broker before retrying this intent.');
    expect(timeline.textContent).toContain('blocked');
    expect(timeline.textContent).toContain('broker_ack #1');
    expect(timeline.textContent).toContain('PROBE_BROKER_BEFORE_RETRY');
    expect(timeline.textContent).toContain('ET');
  });

  it('surfaces projection fallback without hiding the file-backed snapshot', async () => {
    await render(TraderGuidancePaneComponent, {
      inputs: {
        surface: makeOperatorSurfaceFixture(),
        timelineRows: [],
        timelineProjectionAvailable: false,
        timelineCanonicalFallbackRequired: true,
        timelineNotice: 'Projection unavailable; current snapshot remains file-backed.',
      },
    });

    const timeline = screen.getByTestId('trader-guidance-timeline');
    expect(timeline.textContent).toContain('Projection unavailable; current snapshot remains file-backed.');
    expect(timeline.textContent).toContain('No recent projection rows are available for this bot.');
  });
});
