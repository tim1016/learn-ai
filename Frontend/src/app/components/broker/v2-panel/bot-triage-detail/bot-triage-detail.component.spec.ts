import { fireEvent, render, screen } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import { describe, expect, it, vi } from 'vitest';

import { fakeBotPanelView, fakePanelAction } from '../../../../testing/bot-panel-fixtures';
import type { BotPanelView, ReadinessCheckView } from '../lib/broker-v2-panel.types';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import { BotTriageDetailComponent } from './bot-triage-detail.component';

function fakeGate(overrides: Partial<ReadinessCheckView> = {}): ReadinessCheckView {
  return {
    label: 'Market data fresh',
    ready: true,
    explanation: 'The market-data window is current.',
    cure: null,
    authority: 'start_admission',
    operation: 'resume',
    scope: 'bot',
    evidence: {},
    evaluated_at_ms: 1_700_000_001_000,
    ...overrides,
  };
}

async function renderDetail(
  panel: BotPanelView | null = fakeBotPanelView(),
  options: { sid?: string | null; panelError?: Error } = {},
) {
  const sid = options.sid === undefined ? (panel?.strategy_instance_id ?? null) : options.sid;

  const mockPanelService = {
    getPanel: vi.fn(() =>
      options.panelError ? Promise.reject(options.panelError) : Promise.resolve(panel),
    ),
    getEvidence: vi.fn(() => Promise.resolve({ entries: [], next_cursor: null })),
  };

  const view = await render(BotTriageDetailComponent, {
    providers: [
      provideRouter([]),
      { provide: BrokerV2PanelService, useValue: mockPanelService },
    ],
    componentInputs: { broker: 'alpaca', accountId: 'PA9', sid },
  });
  return { ...view, mockPanelService };
}

describe('BotTriageDetailComponent', () => {
  it('invites a selection when no bot is chosen', async () => {
    await renderDetail(null, { sid: null });

    expect(await screen.findByText('No bot selected')).toBeTruthy();
  });

  it('does not read a panel until a bot is selected', async () => {
    const { mockPanelService } = await renderDetail(null, { sid: null });

    expect(mockPanelService.getPanel).not.toHaveBeenCalled();
    expect(mockPanelService.getEvidence).not.toHaveBeenCalled();
  });

  it('renders the trade fact and the failure diagnosis together', async () => {
    await renderDetail(
      fakeBotPanelView({
        mission_verdict: {
          state: 'blocked',
          label: 'Failed to start',
          explanation: 'Admission refused at start_admission.market_data_fresh.',
          next_action: 'Refresh the SPY minute window, then retry start.',
          evaluated_at_ms: 1_700_000_001_000,
        },
        exposure: { SPY: 12 },
        realized_pnl_today: 402.85,
        fills_today: 3,
      }),
    );

    expect(await screen.findByText('Failed to start')).toBeTruthy();
    expect(screen.getByText('Admission refused at start_admission.market_data_fresh.')).toBeTruthy();
    expect(screen.getByText('Refresh the SPY minute window, then retry start.')).toBeTruthy();
    expect(screen.getByText('+12 SPY')).toBeTruthy();
    expect(screen.getByText('+$402.85')).toBeTruthy();
  });

  it('counts admission gates and puts blocked ones first', async () => {
    await renderDetail(
      fakeBotPanelView({
        readiness_ready_count: 1,
        readiness_blocked_count: 1,
        readiness_checks: [
          fakeGate({ label: 'Account custody proven', ready: true }),
          fakeGate({
            label: 'Market data fresh',
            ready: false,
            explanation: 'Last SPY minute bar is 37s beyond tolerance.',
            cure: 'Fetch the current window from Data Lab, then retry.',
          }),
        ],
      }),
    );

    expect(await screen.findByText('1 / 2')).toBeTruthy();
    expect(screen.getByText('Last SPY minute bar is 37s beyond tolerance.')).toBeTruthy();
    expect(screen.getByText('Fetch the current window from Data Lab, then retry.')).toBeTruthy();

    const gates = screen.getAllByRole('listitem').map((item) => item.textContent ?? '');
    expect(gates[0]).toContain('Market data fresh');
  });

  /** Blocked gates carry a cure; ready ones stay one-liners so the pane scans. */
  it('keeps satisfied gates to a single line', async () => {
    await renderDetail(
      fakeBotPanelView({
        readiness_ready_count: 1,
        readiness_blocked_count: 0,
        readiness_checks: [
          fakeGate({ label: 'Account custody proven', ready: true, cure: 'never shown' }),
        ],
      }),
    );

    expect(await screen.findByText('Account custody proven')).toBeTruthy();
    expect(screen.queryByText('The market-data window is current.')).toBeNull();
    expect(screen.queryByText('never shown')).toBeNull();
  });

  it('delegates a presented action upward instead of running it itself', async () => {
    const actionTriggered = vi.fn();
    await render(BotTriageDetailComponent, {
      providers: [
        provideRouter([]),
        {
          provide: BrokerV2PanelService,
          useValue: {
            getPanel: vi.fn(() =>
              Promise.resolve(fakeBotPanelView({ actions: [fakePanelAction('stop')] })),
            ),
            getEvidence: vi.fn(() => Promise.resolve({ entries: [], next_cursor: null })),
          },
        },
      ],
      componentInputs: { broker: 'alpaca', accountId: 'PA9', sid: 'spy-momentum-01' },
      componentOutputs: { actionTriggered: { emit: actionTriggered } as never },
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));

    expect(actionTriggered).toHaveBeenCalledWith(
      expect.objectContaining({ action: expect.objectContaining({ action_id: 'stop' }) }),
    );
  });

  it('offers a retry when the panel cannot be read', async () => {
    const { mockPanelService } = await renderDetail(null, {
      sid: 'spy-momentum-01',
      panelError: new Error('data plane restarting'),
    });

    expect((await screen.findByRole('alert')).textContent).toContain('Bot detail unavailable');

    mockPanelService.getPanel.mockClear();
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));

    await vi.waitFor(() => expect(mockPanelService.getPanel).toHaveBeenCalled());
  });

  /** A stale panel from the previously selected bot must never render as the new one. */
  it('does not render another bot\'s panel while the selected one loads', async () => {
    const view = await renderDetail(fakeBotPanelView({ strategy_instance_id: 'spy-momentum-01' }));
    await screen.findByText('Deployment Validation');

    view.fixture.componentRef.setInput('sid', 'other-bot');
    view.fixture.detectChanges();

    expect(screen.queryByText('Deployment Validation')).toBeNull();
  });
});
