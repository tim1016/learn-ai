import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { MessageService } from 'primeng/api';
import { describe, expect, it, vi } from 'vitest';

import type { ChartBar, ChartFillMarker, PanelAction } from '../../lib/broker-v2-panel.types';
import { BrokerV2PanelService } from '../../lib/broker-v2-panel.service';
import { GalleryLiveStore } from '../lib/gallery-live-store.service';
import type { GalleryBotView, GalleryLiveStatus, GalleryResolution } from '../lib/gallery.types';
import { BotGalleryPageComponent } from './bot-gallery-page.component';
import {
  provideFleetDirectory,
  testLane,
  type FleetDirectoryDouble,
} from '../../../../../fleet/fleet-directory-testing';
import { LANE_FENCE_REFRESH_FAILED_MESSAGE } from '../../../../../fleet/lane-fence';

const BROKER = 'alpaca';
const ACCOUNT_ID = 'PA3';

function bot(overrides: Partial<GalleryBotView> = {}): GalleryBotView {
  return {
    sid: 'sid-1',
    symbol: 'SPY',
    label: 'ORB breakout',
    running: true,
    phase: 'RUNNING',
    desired_state: 'ON_DUTY',
    needs_attention: false,
    realized_pnl_today: 0,
    open_pnl: 0,
    day_pnl: 0,
    session_change_pct: 0,
    fills_today: 0,
    last_bar_at_ms: null,
    primary_action: { action_id: 'stop', label: 'Stop', enabled: true, disabled_reason: null },
    ...overrides,
  };
}

function fakeAction(actionId: 'resume' | 'stop', enabled = true): PanelAction {
  return {
    action_id: actionId,
    label: actionId === 'resume' ? 'Resume' : 'Stop',
    explanation: `${actionId} this bot.`,
    enabled,
    blockers: [],
    confirmation: null,
    revision: 1,
    concurrency_token: `${actionId}-token`,
  };
}

interface FakeGalleryStore {
  bots: ReturnType<typeof signal<GalleryBotView[]>>;
  barsBySymbol: ReturnType<typeof signal<ReadonlyMap<string, readonly ChartBar[]>>>;
  markersBySid: ReturnType<typeof signal<ReadonlyMap<string, readonly ChartFillMarker[]>>>;
  resolution: ReturnType<typeof signal<GalleryResolution>>;
  status: ReturnType<typeof signal<GalleryLiveStatus>>;
  start: ReturnType<typeof vi.fn>;
  stop: ReturnType<typeof vi.fn>;
}

function fakeGalleryStore(overrides: {
  bots?: GalleryBotView[];
  resolution?: GalleryResolution;
  status?: GalleryLiveStatus;
} = {}): FakeGalleryStore {
  return {
    bots: signal<GalleryBotView[]>(overrides.bots ?? []),
    barsBySymbol: signal<ReadonlyMap<string, readonly ChartBar[]>>(new Map()),
    markersBySid: signal<ReadonlyMap<string, readonly ChartFillMarker[]>>(new Map()),
    resolution: signal<GalleryResolution>(overrides.resolution ?? '5s'),
    status: signal<GalleryLiveStatus>(overrides.status ?? 'connecting'),
    start: vi.fn().mockResolvedValue(undefined),
    stop: vi.fn(),
  };
}

interface PanelServiceOverrides {
  getPanel?: ReturnType<typeof vi.fn>;
  runBotAction?: ReturnType<typeof vi.fn>;
  directory?: FleetDirectoryDouble;
}

async function renderPage(store: FakeGalleryStore, overrides: PanelServiceOverrides = {}) {
  const panelService = {
    getPanel: overrides.getPanel ?? vi.fn().mockResolvedValue({ actions: [fakeAction('stop')] }),
    runBotAction:
      overrides.runBotAction ??
      vi.fn().mockResolvedValue({
        action_id: 'stop',
        applied: true,
        revision: 2,
        concurrency_token: 'next-token',
        message: 'Bot stopped.',
      }),
  };
  const messageService = { add: vi.fn() };

  // The double's default lane must resolve for this file's routed clerkId
  // ('clrk_spec', not the shared fixture's TEST_CLERK_ID) so the fence tests
  // exercise a real lane rather than a permanently-missing one.
  const directory =
    overrides.directory ??
    provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec' })],
    });

  TestBed.overrideComponent(BotGalleryPageComponent, {
    set: { providers: [
      { provide: directory.provide, useValue: directory.useValue },
      { provide: GalleryLiveStore, useValue: store }] },
  });

  const view = await render(BotGalleryPageComponent, {
    inputs: { clerkId: 'clrk_spec', broker: BROKER, accountId: ACCOUNT_ID },
    providers: [
      provideRouter([]),
      { provide: BrokerV2PanelService, useValue: panelService },
      { provide: MessageService, useValue: messageService },
    ],
  });

  return { ...view, panelService, messageService };
}

describe('BotGalleryPageComponent', () => {
  it('starts the gallery live store with the routed broker and account', async () => {
    const store = fakeGalleryStore({ status: 'live', bots: [bot()] });

    await renderPage(store);

    expect(store.start).toHaveBeenCalledWith(BROKER, 'clrk_spec', ACCOUNT_ID, 3, 4);
  });

  it('shows a loading skeleton while connecting with no bots yet', async () => {
    const store = fakeGalleryStore({ status: 'connecting', bots: [] });

    await renderPage(store);

    expect(screen.getByLabelText('Loading bot gallery')).toBeTruthy();
    expect(screen.queryByText('No bots yet')).toBeNull();
  });

  it('renders the dock once bots are present', async () => {
    const store = fakeGalleryStore({ status: 'live', bots: [bot()] });

    await renderPage(store);

    expect(screen.getByText('SPY')).toBeTruthy();
    expect(screen.queryByLabelText('Loading bot gallery')).toBeNull();
  });

  it('shows the honest empty state with a link to the bots roster when the account has no bots', async () => {
    const store = fakeGalleryStore({ status: 'live', bots: [] });

    await renderPage(store);

    expect(screen.getByText('No bots yet')).toBeTruthy();
    const link = screen.getByRole('link', { name: 'View bots roster' }) as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe(
      `/brokers/${BROKER}/clerks/clrk_spec/accounts/${ACCOUNT_ID}/bots`,
    );
  });

  it('shows a non-blocking delayed indicator when the feed is stale, and keeps the dock visible', async () => {
    const store = fakeGalleryStore({ status: 'stale', bots: [bot()] });

    await renderPage(store);

    expect(screen.getAllByText('Delayed').length).toBeGreaterThan(0);
    expect(screen.getByText('SPY')).toBeTruthy();
  });

  it('has no page-level toolbar or title — the dock owns the whole view', async () => {
    const store = fakeGalleryStore({ status: 'live', bots: [bot()] });

    await renderPage(store);

    expect(screen.queryByRole('heading', { name: 'Bot gallery' })).toBeNull();
    expect(screen.queryByText('Bot gallery')).toBeNull();
  });

  it('forwards the store connection status down to the dock footer', async () => {
    const store = fakeGalleryStore({ status: 'connecting', bots: [bot()] });

    await renderPage(store);

    expect(screen.getByText('Connecting…')).toBeTruthy();
  });

  it('shows an error banner when the feed has never connected', async () => {
    const store = fakeGalleryStore({ status: 'error', bots: [] });

    await renderPage(store);

    expect(screen.getByRole('alert').textContent).toContain('Gallery feed unavailable');
    expect(screen.queryByText('No running bots')).toBeNull();
  });

  it('routes a confirmed tile quick action through getPanel then runBotAction', async () => {
    const store = fakeGalleryStore({ status: 'live', bots: [bot({ sid: 'sid-1' })] });
    const { panelService } = await renderPage(store);

    fireEvent.click(screen.getByRole('button', { name: /^Stop$/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    await Promise.resolve();
    await Promise.resolve();

    expect(panelService.getPanel).toHaveBeenCalledWith(
      expect.objectContaining({ broker: BROKER, clerkId: 'clrk_spec', accountId: ACCOUNT_ID }),
      'sid-1',
    );
    expect(panelService.runBotAction).toHaveBeenCalledWith(
      expect.objectContaining({ broker: BROKER, clerkId: 'clrk_spec', accountId: ACCOUNT_ID }),
      'sid-1',
      fakeAction('stop'),
    );
  });

  it('marks the sid pending — disabling and aria-busy-ing the tile button — while the action is in flight, and clears it after', async () => {
    const store = fakeGalleryStore({ status: 'live', bots: [bot({ sid: 'sid-1' })] });
    let resolveGetPanel!: (value: { actions: PanelAction[] }) => void;
    const getPanel = vi.fn(
      () => new Promise<{ actions: PanelAction[] }>((resolve) => { resolveGetPanel = resolve; }),
    );
    const { fixture } = await renderPage(store, { getPanel });

    fireEvent.click(screen.getByRole('button', { name: /^Stop$/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    await fixture.whenStable();

    const pendingButton = screen.getByRole('button', { name: /Stop/i }) as HTMLButtonElement;
    expect(pendingButton.getAttribute('aria-busy')).toBe('true');
    expect(pendingButton.disabled).toBe(true);

    resolveGetPanel({ actions: [fakeAction('stop')] });

    await waitFor(() => {
      const settledButton = screen.getByRole('button', { name: /^Stop$/i }) as HTMLButtonElement;
      expect(settledButton.getAttribute('aria-busy')).toBe('false');
      expect(settledButton.disabled).toBe(false);
    });
  });

  it('does not call runBotAction when the refreshed panel no longer offers the action', async () => {
    const store = fakeGalleryStore({ status: 'live', bots: [bot({ sid: 'sid-1' })] });
    const { panelService } = await renderPage(store);
    panelService.getPanel.mockResolvedValue({ actions: [fakeAction('stop', false)] });

    fireEvent.click(screen.getByRole('button', { name: /^Stop$/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    await Promise.resolve();
    await Promise.resolve();

    expect(panelService.runBotAction).not.toHaveBeenCalled();
  });

  it('stops the live store when the page is destroyed', async () => {
    const store = fakeGalleryStore({ status: 'live', bots: [bot()] });
    const { fixture } = await renderPage(store);

    fixture.destroy();

    expect(store.stop).toHaveBeenCalled();
  });

  it('refuses a gallery action whose lane rebound while the tile was on screen', async () => {
    const directory = provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec' })],
    });
    const store = fakeGalleryStore({ status: 'live', bots: [bot({ sid: 'sid-1' })] });
    const runBotAction = vi.fn().mockResolvedValue({ message: 'stopped' });
    const { panelService, messageService } = await renderPage(store, { directory, runBotAction });

    // The operator is shown generation 3, then the coordinator rebinds to 4
    // before they confirm the tile action — exactly what refresh() will start
    // doing.
    directory.rebind({
      observed_at_ms: 1_757_000_000_001,
      clerks: [testLane({ clerk_id: 'clrk_spec', effective_binding_generation: 4 })],
    });
    // `rebind()` only stages the replacement; `refresh()` promotes it to
    // what `lanesOf()`/`lane()` report, like the real service's next load.
    await directory.useValue.refresh?.();

    fireEvent.click(screen.getByRole('button', { name: /^Stop$/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    await Promise.resolve();
    await Promise.resolve();

    expect(panelService.getPanel).not.toHaveBeenCalled();
    expect(runBotAction).not.toHaveBeenCalled();
    expect(messageService.add).toHaveBeenCalledWith(
      expect.objectContaining({
        severity: 'warn',
        detail: expect.stringMatching(/rebound while the action was open/i),
      }),
    );
  });

  it('refuses a gallery action when the lane had no known binding at open, and dispatches nothing', async () => {
    // Cold directory: the lane is present but its binding is unconfirmed, so
    // `openFence` freezes `{bindingGeneration: null, ...}` (#2068, decision
    // 15). A present-but-null lane, not an absent one: an absent lane would
    // also read as "drifted" by laneFenceDrifted, which would mask a deleted
    // enforceability branch behind the drift branch instead of proving it.
    const directory = provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec', effective_binding_generation: null })],
    });
    const store = fakeGalleryStore({ status: 'live', bots: [bot({ sid: 'sid-1' })] });
    const runBotAction = vi.fn().mockResolvedValue({ message: 'stopped' });
    const { panelService, messageService } = await renderPage(store, { directory, runBotAction });

    fireEvent.click(screen.getByRole('button', { name: /^Stop$/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    await Promise.resolve();
    await Promise.resolve();

    expect(panelService.getPanel).not.toHaveBeenCalled();
    expect(runBotAction).not.toHaveBeenCalled();
    expect(messageService.add).toHaveBeenCalledWith(
      expect.objectContaining({
        severity: 'warn',
        detail: expect.stringMatching(/no known binding when the action was opened/i),
      }),
    );
  });

  /**
   * `FleetDirectoryService.refresh()` had no caller before this fix, which is
   * the only reason the pre-freeze click-time fence read was harmless. Now
   * that the fence is frozen at open, a stale-generation refusal must refresh
   * the directory so the operator's next action is minted against a lane
   * they have actually been shown (#2068). `bots-list-page` and
   * `bot-panel-shell` already carried this mitigation; the gallery runs the
   * identical getPanel -> runBotAction round trip and can hit the identical
   * refusal, so it must carry it too.
   */
  it('refreshes the directory after the coordinator refuses a stale generation', async () => {
    const directory = provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec' })],
    });
    const refresh = vi.spyOn(directory.useValue as never, 'refresh');
    const store = fakeGalleryStore({ status: 'live', bots: [bot({ sid: 'sid-1' })] });
    const runBotAction = vi.fn().mockRejectedValue(
      new HttpErrorResponse({
        status: 409,
        error: { reason: 'clerk_binding_generation_conflict', message: 'Expected 3 is not 4.' },
      }),
    );
    await renderPage(store, { directory, runBotAction });

    fireEvent.click(screen.getByRole('button', { name: /^Stop$/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));

    await vi.waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
  });

  /**
   * `refresh()` is a proven-rejecting call (`FleetDirectoryService.refresh` ->
   * `awaitLoaded` throws whenever `/api/broker-clerks` fails). Firing it
   * fire-and-forget with no rejection handler would leave the operator with
   * no signal that the mitigation for a stale-generation refusal didn't
   * take — the directory's `response()` signal stays exactly as stale as it
   * was, and the next action hits the identical refusal with no warning.
   */
  it('tells the operator when the mitigating directory refresh itself fails', async () => {
    const directory = provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec' })],
    });
    directory.useValue.refresh = vi.fn().mockRejectedValue(new Error('directory reload failed'));
    const store = fakeGalleryStore({ status: 'live', bots: [bot({ sid: 'sid-1' })] });
    const runBotAction = vi.fn().mockRejectedValue(
      new HttpErrorResponse({
        status: 409,
        error: { reason: 'clerk_binding_generation_conflict', message: 'Expected 3 is not 4.' },
      }),
    );
    const { messageService } = await renderPage(store, { directory, runBotAction });

    fireEvent.click(screen.getByRole('button', { name: /^Stop$/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));

    await vi.waitFor(() =>
      expect(messageService.add).toHaveBeenCalledWith(
        expect.objectContaining({ severity: 'error', detail: LANE_FENCE_REFRESH_FAILED_MESSAGE }),
      ),
    );
    expect(runBotAction).toHaveBeenCalledTimes(1);
  });
});
