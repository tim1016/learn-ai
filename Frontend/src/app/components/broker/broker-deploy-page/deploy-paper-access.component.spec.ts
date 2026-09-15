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
import { provideFleetDirectory } from '../../../fleet/fleet-directory-testing';
import { resourceTarget } from '../../../fleet/resource-target';

const TARGET = resourceTarget('alpaca', 'clrk_spec', {
  accountId: 'paper-account-1', bindingGeneration: 3, routingEpoch: 7,
});

const AVAILABLE_STRATEGY: DeployBotStrategy = {
  strategy_key: 'ema_crossover_signal',
  label: 'EMA Crossover Signal',
  explanation: 'Validated EMA crossover strategy.',
  validation_case_symbol: 'SPY',
  validation_case_parameters: {},
  golden_validation_scope: false,
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
      inputs: { target: TARGET, accountId: 'paper-account-1', strategy: AVAILABLE_STRATEGY, modeLabel: 'Paper' },
      providers: [
      provideFleetDirectory(),{ provide: BrokerV2PanelService, useValue: service }],
    });

    expect(screen.getByRole('heading', { name: 'Paper access' })).toBeTruthy();
    expect(screen.getByText('Off')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Review & enable Paper' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(service.preparePaperAccess).toHaveBeenCalledWith(
      expect.objectContaining({ broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'paper-account-1' }),
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
      expect.objectContaining({ broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'paper-account-1' }),
      'ema_crossover_signal',
      PLAN,
    );
    const preparedTarget = service.preparePaperAccess.mock.calls[0][0];
    const confirmedTarget = service.confirmPaperAccess.mock.calls[0][0];
    expect(preparedTarget.idempotencyKey).toEqual(expect.any(String));
    expect(confirmedTarget).toBe(preparedTarget);
    expect(screen.getByText('Paper access enabled')).toBeTruthy();
    expect(screen.getByText(/deploying a bot remains a separate action/i)).toBeTruthy();
  });

  it('does not render approval controls for strategies outside the sealed-program gate', async () => {
    const service = panelServiceMock();
    await render(DeployPaperAccessComponent, {
      inputs: { target: TARGET,
        accountId: 'paper-account-1',
        strategy: { ...AVAILABLE_STRATEGY, paper_access_state: 'not_required' },
        modeLabel: 'Paper' as const,
      },
      providers: [{ provide: BrokerV2PanelService, useValue: service }],
    });

    expect(screen.queryByRole('heading', { name: 'Paper access' })).toBeNull();
    expect(service.preparePaperAccess).not.toHaveBeenCalled();
  });

  it('drops a presented review when the same account rebinds to another Clerk', async () => {
    const service = panelServiceMock();
    const { fixture } = await render(DeployPaperAccessComponent, {
      inputs: { target: TARGET, accountId: 'paper-account-1', strategy: AVAILABLE_STRATEGY, modeLabel: 'Paper' },
      providers: [{ provide: BrokerV2PanelService, useValue: service }],
    });

    fireEvent.click(screen.getByRole('button', { name: 'Review & enable Paper' }));
    await fixture.whenStable();
    fixture.detectChanges();
    expect(screen.getByRole('region', { name: 'Review Paper access' })).toBeTruthy();

    fixture.componentRef.setInput('target', resourceTarget('alpaca', 'clrk_other', {
      accountId: 'paper-account-1', bindingGeneration: 3, routingEpoch: 7,
    }));
    fixture.detectChanges();

    expect(screen.queryByRole('region', { name: 'Review Paper access' })).toBeNull();
    expect(service.confirmPaperAccess).not.toHaveBeenCalled();
  });

  it('drops a presented review when the same lane advances its routing epoch', async () => {
    const service = panelServiceMock();
    const { fixture } = await render(DeployPaperAccessComponent, {
      inputs: { target: TARGET, accountId: 'paper-account-1', strategy: AVAILABLE_STRATEGY, modeLabel: 'Paper' },
      providers: [{ provide: BrokerV2PanelService, useValue: service }],
    });

    fireEvent.click(screen.getByRole('button', { name: 'Review & enable Paper' }));
    await fixture.whenStable();
    fixture.detectChanges();
    fixture.componentRef.setInput('target', resourceTarget('alpaca', 'clrk_spec', {
      accountId: 'paper-account-1', bindingGeneration: 4, routingEpoch: 8,
    }));
    fixture.detectChanges();

    expect(screen.queryByRole('region', { name: 'Review Paper access' })).toBeNull();
    expect(service.confirmPaperAccess).not.toHaveBeenCalled();
  });

  it('does not offer approval when backend prerequisites are blocked', async () => {
    const service = panelServiceMock();
    await render(DeployPaperAccessComponent, {
      inputs: { target: TARGET,
        accountId: 'paper-account-1',
        strategy: { ...AVAILABLE_STRATEGY, paper_access_state: 'blocked' },
        modeLabel: 'Paper' as const,
      },
      providers: [{ provide: BrokerV2PanelService, useValue: service }],
    });

    expect(screen.queryByRole('heading', { name: 'Paper access' })).toBeNull();
    expect(service.preparePaperAccess).not.toHaveBeenCalled();
  });

  it('surfaces the receipt git lineage in the audit details', async () => {
    const service = panelServiceMock();
    service.preparePaperAccess.mockResolvedValueOnce({
      ...PLAN,
      evidence: {
        ...PLAN.evidence,
        git_provenance: { commit_sha: 'f'.repeat(40), dirty: true },
      },
    });
    const { fixture } = await render(DeployPaperAccessComponent, {
      inputs: { target: TARGET, accountId: 'paper-account-1', strategy: AVAILABLE_STRATEGY, modeLabel: 'Paper' },
      providers: [{ provide: BrokerV2PanelService, useValue: service }],
    });

    fireEvent.click(screen.getByRole('button', { name: 'Review & enable Paper' }));
    await fixture.whenStable();
    fixture.detectChanges();

    const review = screen.getByRole('region', { name: 'Review Paper access' });
    expect(within(review).getByText('Qualified source')).toBeTruthy();
    expect(within(review).getByText('f'.repeat(40))).toBeTruthy();
    expect(within(review).getByText(/uncommitted edits included/i)).toBeTruthy();
  });

  it('omits the git lineage row when the receipt predates it', async () => {
    const service = panelServiceMock();
    const { fixture } = await render(DeployPaperAccessComponent, {
      inputs: { target: TARGET, accountId: 'paper-account-1', strategy: AVAILABLE_STRATEGY, modeLabel: 'Paper' },
      providers: [{ provide: BrokerV2PanelService, useValue: service }],
    });

    fireEvent.click(screen.getByRole('button', { name: 'Review & enable Paper' }));
    await fixture.whenStable();
    fixture.detectChanges();

    const review = screen.getByRole('region', { name: 'Review Paper access' });
    expect(within(review).queryByText('Qualified source')).toBeNull();
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
      inputs: { target: TARGET, accountId: 'paper-account-1', strategy: AVAILABLE_STRATEGY, modeLabel: 'Paper' },
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

  it('retries a failed confirm with the exact reviewed target and durable key', async () => {
    const service = panelServiceMock();
    service.confirmPaperAccess
      .mockRejectedValueOnce(new Error('connection lost after submit'))
      .mockResolvedValueOnce({ action: 'activated' });
    const { fixture } = await render(DeployPaperAccessComponent, {
      inputs: { target: TARGET, accountId: 'paper-account-1', strategy: AVAILABLE_STRATEGY, modeLabel: 'Paper' },
      providers: [{ provide: BrokerV2PanelService, useValue: service }],
    });

    fireEvent.click(screen.getByRole('button', { name: 'Review & enable Paper' }));
    await fixture.whenStable();
    fireEvent.click(screen.getByRole('button', { name: 'Enable Paper access' }));
    await fixture.whenStable();
    fixture.detectChanges();
    expect(screen.getByRole('button', { name: 'Try enable again' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Try enable again' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(service.confirmPaperAccess).toHaveBeenCalledTimes(2);
    expect(service.confirmPaperAccess.mock.calls[1][0])
      .toBe(service.confirmPaperAccess.mock.calls[0][0]);
    expect(service.confirmPaperAccess.mock.calls[1][0].idempotencyKey)
      .toBe(service.confirmPaperAccess.mock.calls[0][0].idempotencyKey);
  });
  it('words the same grant for the shadow world on a live account', async () => {
    const service = panelServiceMock();
    const { fixture } = await render(DeployPaperAccessComponent, {
      inputs: { target: resourceTarget('alpaca', 'clrk_spec', {
        accountId: '9LIVE0001', bindingGeneration: 3, routingEpoch: 7,
      }),
        accountId: '9LIVE0001',
        strategy: AVAILABLE_STRATEGY,
        modeLabel: 'Shadow' as const,
      },
      providers: [{ provide: BrokerV2PanelService, useValue: service }],
    });

    expect(screen.getByRole('heading', { name: 'Shadow access' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Review & enable Shadow' }));
    await fixture.whenStable();
    fixture.detectChanges();

    const review = screen.getByRole('region', { name: 'Review Shadow access' });
    expect(within(review).getByRole('heading', { name: 'Confirm Shadow access' })).toBeTruthy();
    expect(within(review).getByRole('button', { name: 'Enable Shadow access' })).toBeTruthy();

    fireEvent.click(within(review).getByRole('button', { name: 'Enable Shadow access' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByText('Shadow access enabled')).toBeTruthy();
    // The one word that must not survive onto a real-money account's form.
    expect(document.body.textContent).not.toMatch(/paper/i);
  });

  /** #2106: `target` arrives frozen at drawer-open (`AlpacaDeployDrawerComponent`
   * never re-derives it from a live directory read while the drawer stays
   * open), but a cold directory AT open freezes a null generation, and
   * `commandContextOf` sends no generation check at all for one. Refuse
   * rather than dispatch blind. */
  it('refuses to prepare a review when the lane had no known binding when the drawer opened', async () => {
    const service = panelServiceMock();
    const coldTarget = resourceTarget('alpaca', 'clrk_spec', {
      accountId: 'paper-account-1', bindingGeneration: null, routingEpoch: null,
    });
    await render(DeployPaperAccessComponent, {
      inputs: {
        target: coldTarget, accountId: 'paper-account-1', strategy: AVAILABLE_STRATEGY, modeLabel: 'Paper',
      },
      providers: [provideFleetDirectory(), { provide: BrokerV2PanelService, useValue: service }],
    });

    fireEvent.click(screen.getByRole('button', { name: 'Review & enable Paper' }));

    expect(service.preparePaperAccess).not.toHaveBeenCalled();
    expect(await screen.findByText(/no known binding when the action was opened/i)).toBeTruthy();
  });
});
