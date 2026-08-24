import { HttpErrorResponse } from '@angular/common/http';
import { fireEvent, render, screen, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { formatTimestampDisplay } from '../../../shared/timestamp';
import {
  BrokerV2PanelService,
  type DeployBotStrategy,
  type PaperAccessPlan,
} from '../v2-panel/lib/broker-v2-panel.service';
import { DeployPaperAccessComponent } from './deploy-paper-access.component';

const AVAILABLE_STRATEGY: DeployBotStrategy = {
  strategy_key: 'ema_crossover_signal',
  label: 'EMA Crossover Signal',
  explanation: 'Validated EMA crossover strategy.',
  validation_case_symbol: 'SPY',
  evidence_status: 'blocked',
  paper_access_state: 'available',
  selectable: false,
  admissible_modes: ['dry_run'],
  override_explanation: null,
  blocked_explanation: 'Paper trading is not enabled for this strategy on this account yet.',
  params_schema: { properties: {}, required: [] },
};

const PLAN: PaperAccessPlan = {
  schema_version: 1,
  plan_id: 'a'.repeat(64),
  confirmation_token: 'a'.repeat(64),
  program_key: 'ema_crossover_signal',
  account_id: 'paper-account-1',
  actor: 'operator',
  reason: 'Enable Paper access from the Alpaca Deploy page.',
  created_at_ms: 1_788_000_000_000,
  expires_at_ms: 1_788_000_120_000,
  ledger_path: '/tmp/test-ledger.json',
  expected_ledger_head_hash: null,
  evidence: {
    validation_event_id: 'validation-event-1',
    validation_snapshot_sha256: 'b'.repeat(64),
    program_version: '1',
    golden_trace_root: 'c'.repeat(64),
    running_artifact_digest: 'd'.repeat(64),
    qualification_receipt_hash: 'e'.repeat(64),
    qualification_suite: 'sealed-program',
    qualified_at_ms: 1_788_000_000_000,
  },
};

function panelServiceMock() {
  return {
    preparePaperAccess: vi.fn().mockResolvedValue(PLAN),
    confirmPaperAccess: vi.fn().mockResolvedValue({ action: 'activated' }),
  };
}

describe('DeployPaperAccessComponent', () => {
  it('prepares a review and requires a separate explicit confirmation', async () => {
    const service = panelServiceMock();
    const { fixture } = await render(DeployPaperAccessComponent, {
      inputs: { accountId: 'paper-account-1', strategy: AVAILABLE_STRATEGY },
      providers: [{ provide: BrokerV2PanelService, useValue: service }],
    });

    expect(screen.getByRole('heading', { name: 'Paper access' })).toBeTruthy();
    expect(screen.getByText('Off')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Review & enable Paper' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(service.preparePaperAccess).toHaveBeenCalledWith(
      'alpaca',
      'paper-account-1',
      'ema_crossover_signal',
      'Enable Paper access from the Alpaca Deploy page.',
    );
    const review = screen.getByRole('region', { name: 'Review Paper access' });
    expect(within(review).getByRole('heading', { name: 'Confirm Paper access' })).toBeTruthy();
    expect(within(review).getByText(/does not deploy a bot or place an order/i)).toBeTruthy();
    expect(within(review).getByText('paper-account-1')).toBeTruthy();
    expect(within(review).getByText(formatTimestampDisplay(PLAN.expires_at_ms, {
      mode: 'local',
      granularity: 'time',
    }))).toBeTruthy();
    expect(service.confirmPaperAccess).not.toHaveBeenCalled();

    fireEvent.click(within(review).getByRole('button', { name: 'Enable Paper access' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(service.confirmPaperAccess).toHaveBeenCalledWith(
      'alpaca',
      'paper-account-1',
      'ema_crossover_signal',
      PLAN,
    );
    expect(screen.getByText('Paper access enabled')).toBeTruthy();
    expect(screen.getByText(/deploying a bot remains a separate action/i)).toBeTruthy();
  });

  it('does not render approval controls for strategies outside the sealed-program gate', async () => {
    const service = panelServiceMock();
    await render(DeployPaperAccessComponent, {
      inputs: {
        accountId: 'paper-account-1',
        strategy: { ...AVAILABLE_STRATEGY, paper_access_state: 'not_required' },
      },
      providers: [{ provide: BrokerV2PanelService, useValue: service }],
    });

    expect(screen.queryByRole('heading', { name: 'Paper access' })).toBeNull();
    expect(service.preparePaperAccess).not.toHaveBeenCalled();
  });

  it('does not offer approval when backend prerequisites are blocked', async () => {
    const service = panelServiceMock();
    await render(DeployPaperAccessComponent, {
      inputs: {
        accountId: 'paper-account-1',
        strategy: { ...AVAILABLE_STRATEGY, paper_access_state: 'blocked' },
      },
      providers: [{ provide: BrokerV2PanelService, useValue: service }],
    });

    expect(screen.queryByRole('heading', { name: 'Paper access' })).toBeNull();
    expect(service.preparePaperAccess).not.toHaveBeenCalled();
  });

  it('renders the backend-authored refusal and offers a fresh review', async () => {
    const service = panelServiceMock();
    service.preparePaperAccess.mockRejectedValueOnce(
      new HttpErrorResponse({
        status: 409,
        error: {
          detail: {
            message: 'Paper access could not be changed.',
            why: 'The strategy validation proof is no longer current.',
            next_action: 'Validate the strategy again, then prepare a fresh review.',
          },
        },
      }),
    );
    const { fixture } = await render(DeployPaperAccessComponent, {
      inputs: { accountId: 'paper-account-1', strategy: AVAILABLE_STRATEGY },
      providers: [{ provide: BrokerV2PanelService, useValue: service }],
    });

    fireEvent.click(screen.getByRole('button', { name: 'Review & enable Paper' }));
    await fixture.whenStable();
    fixture.detectChanges();

    const alert = screen.getByRole('alert');
    expect(within(alert).getByText('Paper access could not be changed.')).toBeTruthy();
    expect(within(alert).getByText(/validation proof is no longer current/i)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Try review again' })).toBeTruthy();
  });
});
