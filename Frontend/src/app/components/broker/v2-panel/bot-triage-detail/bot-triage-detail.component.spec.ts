import { fireEvent, render, screen, within } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import { describe, expect, it, vi } from 'vitest';

import { fakeBotPanelView, fakePanelAction } from '../../../../testing/bot-panel-fixtures';
import type {
  BotPanelView,
  ChartLiveResponse,
  ReadinessCheckView,
} from '../lib/broker-v2-panel.types';
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

/**
 * A complete `ChartLiveResponse` — typed against the generated contract so a
 * renamed or newly-required field fails the build instead of passing silently.
 */
function fakeLiveChart(overrides: Partial<ChartLiveResponse> = {}): ChartLiveResponse {
  return {
    as_of_ms: 1_700_000_000_000,
    bars: [],
    fill_markers: [],
    overlay_notices: [],
    resolution: '1m',
    strategy_instance_id: 'spy-momentum-01',
    symbol: 'SPY',
    trading_date_open_ms: 1_700_000_000_000,
    trading_date_close_ms: 1_700_023_400_000,
    ...overrides,
  };
}

/**
 * Command availability and the custody journal live on the Operator lens now;
 * the pane opens on Trader. Tests about that evidence must switch first.
 */
async function showOperator(): Promise<void> {
  fireEvent.click(await screen.findByRole('tab', { name: 'Operator' }));
}

async function renderDetail(
  panel: BotPanelView | null = fakeBotPanelView(),
  options: {
    sid?: string | null;
    panelError?: Error;
    evidenceError?: Error;
    liveChart?: ChartLiveResponse;
  } = {},
) {
  const sid = options.sid === undefined ? (panel?.strategy_instance_id ?? null) : options.sid;

  const mockPanelService = {
    getPanel: vi.fn(() =>
      options.panelError ? Promise.reject(options.panelError) : Promise.resolve(panel),
    ),
    getEvidence: vi.fn(() =>
      options.evidenceError
        ? Promise.reject(options.evidenceError)
        : Promise.resolve({ entries: [], next_cursor: null }),
    ),
    getLiveChart: vi.fn(() => Promise.resolve(options.liveChart ?? fakeLiveChart())),
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

  it('puts unavailable commands first, with their reason', async () => {
    await renderDetail(
      fakeBotPanelView({
        readiness_ready_count: 1,
        readiness_blocked_count: 1,
        readiness_checks: [
          fakeGate({ label: 'Stop', ready: true, operation: 'stop' }),
          fakeGate({
            label: 'Resume',
            ready: false,
            operation: 'resume',
            explanation: 'Last SPY minute bar is 37s beyond tolerance.',
            cure: 'Fetch the current window from Data Lab, then retry.',
          }),
        ],
      }),
    );

    await showOperator();

    expect(await screen.findByText('Last SPY minute bar is 37s beyond tolerance.')).toBeTruthy();
    expect(screen.getByText('Fetch the current window from Data Lab, then retry.')).toBeTruthy();

    // Scoped to the region: `recent_decisions` also renders list items.
    const card = screen.getByRole('region', { name: 'Command availability' });
    const rows = within(card)
      .getAllByRole('listitem')
      .map((item) => item.textContent ?? '');
    expect(rows[0]).toContain('Resume');
  });

  /**
   * `readiness_checks` is one row per panel action with `ready = action.enabled`
   * (`panel_projection_service._readiness_checks`), and the lifecycle commands
   * are mutually exclusive — a healthy running bot always reports several as
   * unavailable. Presenting that as a failed-gate count painted every normal
   * bot negative, so no such tile exists.
   */
  it('does not report an unavailable command as a failed gate', async () => {
    await renderDetail(
      fakeBotPanelView({
        mission_verdict: {
          state: 'working',
          label: 'Working',
          explanation: 'Running under Account Clerk custody.',
          next_action: null,
          evaluated_at_ms: 1_700_000_001_000,
        },
        readiness_ready_count: 1,
        readiness_blocked_count: 2,
        readiness_checks: [
          fakeGate({ label: 'Stop', ready: true, operation: 'stop' }),
          fakeGate({ label: 'Resume', ready: false, operation: 'resume' }),
          fakeGate({ label: 'Continue', ready: false, operation: 'continue' }),
        ],
      }),
    );

    expect(await screen.findByText('Working')).toBeTruthy();
    expect(screen.queryByText('1 / 3')).toBeNull();
    expect(screen.queryByText('Gates')).toBeNull();
  });

  /** Unavailable rows carry a reason; available ones stay one-liners. */
  it('keeps available commands to a single line', async () => {
    await renderDetail(
      fakeBotPanelView({
        readiness_ready_count: 1,
        readiness_blocked_count: 0,
        readiness_checks: [
          fakeGate({ label: 'Stop', ready: true, operation: 'stop', cure: 'never shown' }),
        ],
      }),
    );

    await showOperator();

    const card = screen.getByRole('region', { name: 'Command availability' });
    expect(within(card).getByText('Stop')).toBeTruthy();
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
            getLiveChart: vi.fn(() => Promise.resolve(fakeLiveChart())),
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

  /**
   * `read_evidence_page` appends an `EvidenceAuditEntry` per call, asserting an
   * operator read at that instant, into a log nothing prunes. Polling it would
   * forge those assertions, so the timer must move the panel only — and the
   * Trader default keeps the audit read parked entirely.
   */
  it('polls the panel but never the audit-logged evidence read', async () => {
    vi.useFakeTimers();
    try {
      const { mockPanelService, fixture } = await renderDetail();
      await vi.waitFor(() => expect(mockPanelService.getPanel).toHaveBeenCalled());

      const panelReads = mockPanelService.getPanel.mock.calls.length;
      // Trader is the default lens; the audit-logged evidence read is parked off it.
      expect(mockPanelService.getEvidence).not.toHaveBeenCalled();

      await vi.advanceTimersByTimeAsync(46_000);
      fixture.detectChanges();
      await fixture.whenStable();

      expect(mockPanelService.getPanel.mock.calls.length).toBeGreaterThan(panelReads);
      expect(mockPanelService.getEvidence).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  /**
   * The Trader default hides the custody journal, so reading it there would
   * stamp an audit assertion the operator never saw. The read is parked until
   * the Operator lens is opened — the first genuine act.
   */
  it('reads the audit-logged evidence only once the Operator lens is opened', async () => {
    const { mockPanelService } = await renderDetail();
    await screen.findByRole('tab', { name: 'Trader' });

    expect(mockPanelService.getEvidence).not.toHaveBeenCalled();

    await showOperator();

    await vi.waitFor(() => expect(mockPanelService.getEvidence).toHaveBeenCalled());
  });

  it('re-reads evidence when the operator acts, since that read is genuine', async () => {
    const view = await renderDetail();
    await showOperator();
    await vi.waitFor(() => expect(view.mockPanelService.getEvidence).toHaveBeenCalled());
    const before = view.mockPanelService.getEvidence.mock.calls.length;

    view.fixture.componentRef.setInput('refreshToken', 1);
    view.fixture.detectChanges();

    await vi.waitFor(() =>
      expect(view.mockPanelService.getEvidence.mock.calls.length).toBeGreaterThan(before),
    );
  });

  /**
   * A refresh is a `reload()`, not a params change. Angular treats a params
   * change as a new request and drops the resource's prior value, which blanked
   * `view()` and replaced the whole pane with the loading placeholder — a
   * visible flicker every 15s that also tore down any open confirmation.
   */
  it('keeps the current panel on screen while a refresh is in flight', async () => {
    vi.useFakeTimers();
    try {
      const panel = fakeBotPanelView();
      // The second read never settles, so the assertions below observe the
      // pane *during* the refresh — the only moment the bug was visible.
      const getPanel = vi
        .fn()
        .mockImplementationOnce(() => Promise.resolve(panel))
        .mockImplementation(() => new Promise<BotPanelView>(() => {}));

      const { fixture } = await render(BotTriageDetailComponent, {
        providers: [
          provideRouter([]),
          {
            provide: BrokerV2PanelService,
            useValue: {
              getPanel,
              getEvidence: vi.fn(() => Promise.resolve({ entries: [], next_cursor: null })),
              getLiveChart: vi.fn(() => Promise.resolve(fakeLiveChart())),
            },
          },
        ],
        componentInputs: {
          broker: 'alpaca',
          accountId: 'PA9',
          sid: panel.strategy_instance_id,
        },
      });

      await vi.waitFor(() => expect(getPanel).toHaveBeenCalledTimes(1));
      fixture.detectChanges();
      expect(screen.queryByText('Deployment Validation')).toBeTruthy();

      await vi.advanceTimersByTimeAsync(16_000);
      fixture.detectChanges();

      expect(getPanel).toHaveBeenCalledTimes(2);
      expect(screen.queryByText(/^Loading /)).toBeNull();
      expect(screen.queryByText('Deployment Validation')).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  /**
   * `JournalTailComponent` renders "No journal entries." for a null page, so
   * passing a failed read through it would convert unavailable custody evidence
   * into an affirmative empty-audit claim.
   */
  it('reports a failed custody read instead of an empty journal', async () => {
    await renderDetail(fakeBotPanelView(), {
      evidenceError: new Error('evidence store unavailable'),
    });

    await showOperator();

    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('could not be read');
    expect(screen.queryByText('No journal entries.')).toBeNull();
  });

  /** A stale panel from the previously selected bot must never render as the new one. */
  it('does not render another bot\'s panel while the selected one loads', async () => {
    const view = await renderDetail(fakeBotPanelView({ strategy_instance_id: 'spy-momentum-01' }));
    await screen.findByText('Deployment Validation');

    view.fixture.componentRef.setInput('sid', 'other-bot');
    view.fixture.detectChanges();

    expect(screen.queryByText('Deployment Validation')).toBeNull();
  });
  it('opens on the trader lens, with command availability one click away', async () => {
    await renderDetail();

    expect(await screen.findByRole('tab', { name: 'Trader' })).toHaveProperty(
      'ariaSelected',
      'true',
    );
    expect(screen.queryByRole('region', { name: 'Command availability' })).toBeNull();

    await showOperator();

    expect(screen.getByRole('region', { name: 'Command availability' })).toBeTruthy();
  });

  /**
   * The rail is a keyboard list an operator arrows through. Fetching a tape per
   * keystroke would open — and immediately abandon — one chart request per row,
   * which is the cost that kept a chart off this pane before.
   */
  it('waits for the selection to settle before reading a tape', async () => {
    vi.useFakeTimers();
    try {
      const { mockPanelService, fixture } = await renderDetail();

      await vi.advanceTimersByTimeAsync(100);
      expect(mockPanelService.getLiveChart).not.toHaveBeenCalled();

      await vi.advanceTimersByTimeAsync(400);
      fixture.detectChanges();
      await fixture.whenStable();

      expect(mockPanelService.getLiveChart).toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it('reads no tape at all while the operator lens is showing', async () => {
    vi.useFakeTimers();
    try {
      const { mockPanelService, fixture } = await renderDetail();
      await showOperator();

      await vi.advanceTimersByTimeAsync(2_000);
      fixture.detectChanges();
      await fixture.whenStable();

      expect(mockPanelService.getLiveChart).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  /**
   * Seven stacked buttons — six of them greyed — is what this pane looked like
   * for a bot that was simply idle. The lead command and the next runnable one
   * stay in the open; the rest fold away.
   */
  it('leads with one command and folds the rest into the overflow', async () => {
    await renderDetail(
      fakeBotPanelView({
        primary_action_by_lens: { trader: 'resume', operator: null },
        actions: [
          fakePanelAction('resume', { enabled: false }),
          fakePanelAction('reconcile_now', { label: 'Reconcile now' }),
          fakePanelAction('prepare_safe_flatten', {
            label: 'Prepare safe flatten',
            enabled: false,
          }),
          fakePanelAction('stop_bot_decisions', {
            label: 'Stop bot decisions',
            enabled: false,
          }),
        ],
      }),
    );

    expect(await screen.findByRole('button', { name: 'Resume' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Reconcile now' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'More commands (2)' })).toBeTruthy();
  });

  /**
   * Only the active tab is tabbable, so a keyboard-only operator reaches the
   * other lens through the WAI-ARIA arrow-key pattern, not the Tab key.
   */
  it('switches lens with the arrow keys, not only the pointer', async () => {
    await renderDetail();

    const trader = await screen.findByRole('tab', { name: 'Trader' });
    const operator = screen.getByRole('tab', { name: 'Operator' });
    expect(trader).toHaveProperty('ariaSelected', 'true');

    fireEvent.keyDown(trader, { key: 'ArrowRight' });

    expect(operator).toHaveProperty('ariaSelected', 'true');
    expect(screen.getByRole('region', { name: 'Command availability' })).toBeTruthy();

    fireEvent.keyDown(operator, { key: 'ArrowLeft' });

    expect(trader).toHaveProperty('ariaSelected', 'true');
    expect(screen.queryByRole('region', { name: 'Command availability' })).toBeNull();
  });

  /**
   * A Polygon bar is display fallback, not live data (ADR 0032 §2). The tape
   * must surface the server's honest notice rather than pass it off as live.
   */
  it('surfaces the Polygon fallback notice on the tape', async () => {
    vi.useFakeTimers();
    try {
      const { fixture } = await renderDetail(fakeBotPanelView(), {
        liveChart: fakeLiveChart({
          overlay_notices: [
            {
              code: 'live_feed_unavailable',
              message: 'Live feed unavailable — showing Polygon (delayed).',
              source: 'polygon',
            },
          ],
        }),
      });

      // Let the debounced tape selection settle, then flush the resolved read.
      await vi.advanceTimersByTimeAsync(400);
      fixture.detectChanges();
      await fixture.whenStable();
      fixture.detectChanges();

      expect(
        screen.getByText('Live feed unavailable — showing Polygon (delayed).'),
      ).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });
});
