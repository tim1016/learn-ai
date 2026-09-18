import { fireEvent, render, screen } from '@testing-library/angular';
import { HttpErrorResponse } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { describe, expect, it, vi } from 'vitest';
import { MessageService } from 'primeng/api';

import { ActiveLensBridgeService } from '../../../../shared/lens/active-lens-bridge.service';
import {
  fakeBotPanelView,
  fakeCatalogBot,
  fakePanelAction,
} from '../../../../testing/bot-panel-fixtures';
import { BotsPageActionsBridgeService } from '../../../brokers/alpaca-workspace/bots-page-actions-bridge.service';
import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import type { BotCatalogView, PanelAction } from '../lib/broker-v2-panel.types';
import { BotsListPageComponent } from './bots-list-page.component';
import {
  provideFleetDirectory,
  testLane,
  type FleetDirectoryDouble,
} from '../../../../fleet/fleet-directory-testing';
import { LANE_FENCE_REFRESH_FAILED_MESSAGE } from '../../../../fleet/lane-fence';
import type { ResourceTarget } from '../../../../fleet/resource-target';

async function renderPage(
  bots: BotCatalogView[] = [],
  overrides: {
    routeAccountId?: string;
    getCatalog?: (target: ResourceTarget) => Promise<BotCatalogView[]>;
    panelActions?: PanelAction[];
    directory?: FleetDirectoryDouble;
    runBotAction?: ReturnType<typeof vi.fn>;
  } = {},
) {
  const mockPanelService = {
    getCatalog: overrides.getCatalog ?? (() => Promise.resolve(bots)),
    getDeployView: vi.fn(() => new Promise<never>(() => undefined)),
    getPanel: vi.fn((_target: ResourceTarget, sid: string) =>
      Promise.resolve(
        fakeBotPanelView({ strategy_instance_id: sid, actions: overrides.panelActions ?? [] }),
      ),
    ),
    getEvidence: vi.fn((_target: ResourceTarget, _sid: string) =>
      Promise.resolve({ entries: [], next_cursor: null }),
    ),
    runBotAction:
      overrides.runBotAction ??
      vi.fn(() =>
        Promise.resolve({
          action_id: 'resume',
          applied: true,
          revision: 1,
          concurrency_token: 'next-token',
          message: 'ok',
        }),
      ),
  };

  const mockMessageService = { add: vi.fn() };

  // The double's default lane must resolve for this file's routed clerkId
  // ('clrk_spec', not the shared fixture's TEST_CLERK_ID) so the fence tests
  // exercise a real lane rather than a permanently-missing one.
  const directory =
    overrides.directory ??
    provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec' })],
    });

  const view = await render(BotsListPageComponent, {
    providers: [
      { provide: directory.provide, useValue: directory.useValue },
      provideRouter([]),
      { provide: BrokerV2PanelService, useValue: mockPanelService },
      { provide: MessageService, useValue: mockMessageService },
    ],
    componentInputs: { broker: 'alpaca', clerkId: 'clrk_spec', accountId: overrides.routeAccountId ?? 'PA9' },
  });
  return { ...view, mockPanelService, mockMessageService };
}

describe('BotsListPageComponent', () => {

  it('leaves the account, its mode and Deploy to the workspace header above it', async () => {
    // #2185: the roster used to repeat the account strip, the lane pill and a
    // Deploy button the workspace header now owns once for every tab.
    await renderPage([fakeCatalogBot()]);

    expect(screen.queryByLabelText('Alpaca account posture')).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Alpaca bots' })).toBeNull();
    expect(screen.queryByRole('button', { name: /Deploy strategy/i })).toBeNull();
    expect(screen.queryByRole('link', { name: /gallery/i })).toBeNull();
  });

  it('titles the rail row with its strategy and names the bot below it', async () => {
    await renderPage([
      fakeCatalogBot({
        strategy_instance_id: 'spy-bot',
        strategy_label: 'EMA Crossover Signal',
        status_label: 'Working',
        phase: 'ON_DUTY',
        running: true,
      }),
    ]);

    expect(await screen.findByRole('heading', { name: 'Running · 1' })).toBeTruthy();
    expect(screen.getByText('EMA Crossover Signal')).toBeTruthy();
    // "Working" is the pulsing dot's job now; the row prints the bot's name.
    expect(screen.getByText('spy-bot')).toBeTruthy();
    expect(screen.queryByText(/^Working ·/)).toBeNull();
  });

  it('names attention state on the rail row rather than relying on colour', async () => {
    await renderPage([fakeCatalogBot({ needs_attention: true })]);

    expect(
      await screen.findByRole('button', { name: /spy-momentum-01, needs attention/ }),
    ).toBeTruthy();
  });

  it('presents the selected bot\'s panel actions in the detail pane', async () => {
    await renderPage([fakeCatalogBot()], { panelActions: [fakePanelAction('stop')] });

    expect(await screen.findByRole('button', { name: 'Stop' })).toBeTruthy();
  });

  it('keeps the fee disclaimer beside the P&L it qualifies', async () => {
    await renderPage();

    expect(await screen.findByText(/Fees not reported/i)).toBeTruthy();
  });

  it('auto-selects the most urgent bot so the detail pane is never blank', async () => {
    await renderPage([
      fakeCatalogBot({ strategy_instance_id: 'calm-bot' }),
      fakeCatalogBot({ strategy_instance_id: 'urgent-bot', needs_attention: true }),
    ]);

    const selected = await screen.findByRole('button', { name: /urgent-bot/ });
    expect(selected.getAttribute('aria-current')).toBe('true');
  });

  it('renders empty state message when no bots', async () => {
    await renderPage([]);

    expect(await screen.findByText(/No Alpaca bots yet/i)).toBeTruthy();
  });

  it('renders snapshot freshness and registers its refresh command with the workspace header', async () => {
    const view = await renderPage([fakeCatalogBot()]);

    expect((await screen.findAllByText(/Updated/i)).length).toBeGreaterThan(0);
    const bridge = view.fixture.debugElement.injector.get(BotsPageActionsBridgeService);
    await vi.waitFor(() => expect(bridge.host()).not.toBeNull());
    expect(typeof bridge.host()?.refresh).toBe('function');
  });

  it('renders the retry state when a transient catalog load fails', async () => {
    await renderPage([], {
      getCatalog: () => Promise.reject(new Error('data plane restarting')),
    });

    expect((await screen.findByRole('alert')).textContent).toContain('Bots unavailable');
  });

  it('never carries a last-good roster across an account route change', async () => {
    let resolvePa10!: (bots: BotCatalogView[]) => void;
    const pa10Catalog = new Promise<BotCatalogView[]>((resolve) => {
      resolvePa10 = resolve;
    });
    const getCatalog = vi.fn((target: ResourceTarget) => {
      if (target.accountId === 'PA10') return pa10Catalog;
      return Promise.resolve([
        fakeCatalogBot({
          strategy_instance_id: 'pa9-bot',
          account_id: target.accountId ?? '',
        }),
      ]);
    });
    const view = await renderPage([], { getCatalog });
    expect(await screen.findByText('pa9-bot')).toBeTruthy();

    view.fixture.componentRef.setInput('accountId', 'PA10');
    view.fixture.detectChanges();

    await vi.waitFor(() => expect(getCatalog).toHaveBeenCalledWith(
      expect.objectContaining({ broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'PA10' }),
    ));
    expect(screen.queryByText('pa9-bot')).toBeNull();

    resolvePa10([
      fakeCatalogBot({ strategy_instance_id: 'pa10-bot', account_id: 'PA10' }),
    ]);
    expect(await screen.findByText('pa10-bot')).toBeTruthy();
  });

  it('does not carry a same-account roster across a Clerk route change', async () => {
    let resolveSecond!: (bots: BotCatalogView[]) => void;
    const delayedSecond = new Promise<BotCatalogView[]>((resolve) => {
      resolveSecond = resolve;
    });
    const getCatalog = vi.fn((target: ResourceTarget) =>
      target.clerkId === 'clrk-b'
        ? delayedSecond
        : Promise.resolve([fakeCatalogBot({ strategy_instance_id: 'clrk-a-bot' })]),
    );
    const view = await renderPage([], { getCatalog });
    expect(await screen.findByText('clrk-a-bot')).toBeTruthy();

    view.fixture.componentRef.setInput('clerkId', 'clrk-b');
    view.fixture.detectChanges();
    await vi.waitFor(() => expect(getCatalog).toHaveBeenCalledWith(
      expect.objectContaining({ clerkId: 'clrk-b', accountId: 'PA9' }),
    ));
    expect(screen.queryByText('clrk-a-bot')).toBeNull();

    resolveSecond([fakeCatalogBot({ strategy_instance_id: 'clrk-b-bot' })]);
    expect(await screen.findByText('clrk-b-bot')).toBeTruthy();
  });

  it('does not publish an old Clerk catalog after its late response settles', async () => {
    let resolveOld!: (bots: BotCatalogView[]) => void;
    const oldCatalog = new Promise<BotCatalogView[]>((resolve) => { resolveOld = resolve; });
    const getCatalog = vi.fn((target: ResourceTarget) =>
      target.clerkId === 'clrk_spec'
        ? oldCatalog
        : Promise.resolve([fakeCatalogBot({ strategy_instance_id: 'clrk-b-current' })]),
    );
    const view = await renderPage([], { getCatalog });

    view.fixture.componentRef.setInput('clerkId', 'clrk-b');
    view.fixture.detectChanges();
    expect(await screen.findByText('clrk-b-current')).toBeTruthy();

    resolveOld([fakeCatalogBot({ strategy_instance_id: 'clrk-a-late' })]);
    await view.fixture.whenStable();

    expect(screen.queryByText('clrk-a-late')).toBeNull();
    expect(screen.getAllByText('clrk-b-current').length).toBeGreaterThan(0);
  });

  it('does not toast or reload the new Clerk when an old-lane action settles', async () => {
    let settleAction!: () => void;
    const getCatalog = vi.fn(() => Promise.resolve([fakeCatalogBot()]));
    const view = await renderPage([fakeCatalogBot()], {
      getCatalog,
      panelActions: [fakePanelAction('stop')],
    });
    view.mockPanelService.runBotAction.mockImplementationOnce(() =>
      new Promise((resolve) => { settleAction = () => resolve({ message: 'old result' } as never); }),
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));
    await vi.waitFor(() => expect(view.mockPanelService.runBotAction).toHaveBeenCalledOnce());
    view.fixture.componentRef.setInput('clerkId', 'clrk-b');
    view.fixture.detectChanges();
    await vi.waitFor(() => expect(getCatalog).toHaveBeenCalledTimes(2));
    const readsBeforeSettlement = getCatalog.mock.calls.length;

    settleAction();
    await view.fixture.whenStable();

    expect(view.mockMessageService.add).not.toHaveBeenCalled();
    expect(getCatalog).toHaveBeenCalledTimes(readsBeforeSettlement);
  });

  /**
   * The triage board reads one panel per *selection*. An action must still
   * submit straight from the action the pane already holds — no extra
   * preflight read between the click and the POST.
   */
  it('submits the presented action without an extra panel preflight', async () => {
    const view = await renderPage([fakeCatalogBot()], { panelActions: [fakePanelAction('stop')] });

    const stop = await screen.findByRole('button', { name: 'Stop' });
    const readsBeforeClick = view.mockPanelService.getPanel.mock.calls.length;
    fireEvent.click(stop);

    await vi.waitFor(() => expect(view.mockPanelService.runBotAction).toHaveBeenCalledOnce());
    expect(view.mockPanelService.runBotAction).toHaveBeenCalledWith(
      expect.objectContaining({ broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'PA9' }),
      'spy-momentum-01',
      expect.objectContaining({ action_id: 'stop', concurrency_token: 'stop-token' }),
      null,
    );
    expect(view.mockPanelService.getPanel.mock.calls.length).toBe(readsBeforeClick);
    expect(view.mockMessageService.add).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'success', detail: 'ok' }),
    );
  });

  /**
   * The detail pane's journal read is audit-logged: `read_evidence_page`
   * appends an `EvidenceAuditEntry` naming the operator and the instant. The
   * refresh token is page-global, so bumping it after the operator moved on
   * would assert they read the *newly selected* bot's evidence, which they
   * never did.
   */
  it('does not refresh another bot\'s audited evidence when an action settles', async () => {
    let settleAction = (): void => undefined;
    const view = await renderPage(
      [
        fakeCatalogBot({ strategy_instance_id: 'bot-a' }),
        fakeCatalogBot({ strategy_instance_id: 'bot-b' }),
      ],
      { panelActions: [fakePanelAction('stop')] },
    );
    view.mockPanelService.runBotAction.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          settleAction = () => resolve({ message: 'ok' } as never);
        }),
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));
    await vi.waitFor(() => expect(view.mockPanelService.runBotAction).toHaveBeenCalledOnce());

    // The operator moves on to another bot while that action is still running,
    // and opens its Operator lens — the only lens that reads the audit-logged
    // custody journal — so bot-b has a genuine baseline read.
    fireEvent.click(await screen.findByRole('button', { name: /bot-b/ }));
    const lensBridge = view.fixture.debugElement.injector.get(ActiveLensBridgeService);
    await vi.waitFor(() => expect(lensBridge.host()).not.toBeNull());
    lensBridge.host()?.select('operator');
    await vi.waitFor(() =>
      expect(view.mockPanelService.getEvidence).toHaveBeenCalledWith(
        expect.objectContaining({ broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'PA9' }),
        'bot-b',
        expect.anything(),
      ),
    );
    const readsOfB = view.mockPanelService.getEvidence.mock.calls.filter(
      (call) => call[1] === 'bot-b',
    ).length;

    settleAction();
    await vi.waitFor(() => expect(view.mockMessageService.add).toHaveBeenCalled());

    const readsOfBAfter = view.mockPanelService.getEvidence.mock.calls.filter(
      (call) => call[1] === 'bot-b',
    ).length;
    expect(readsOfBAfter).toBe(readsOfB);
  });

  it('toasts the backend-authored reason and refreshes the fleet on a rejected action', async () => {
    const view = await renderPage([fakeCatalogBot()], {
      panelActions: [fakePanelAction('resume')],
    });
    view.mockPanelService.runBotAction.mockRejectedValueOnce(
      new HttpErrorResponse({
        status: 409,
        error: {
          detail: {
            action_id: 'resume',
            outcome: 'conflict',
            receipt_id: null,
            recorded_at_ms: 1_700_000_000_000,
            message: 'This bot is no longer ready to resume.',
            why: 'Its custody state changed after this button was shown.',
          },
        },
      }),
    );

    fireEvent.click(await screen.findByRole('button', { name: 'Resume' }));

    await vi.waitFor(() => expect(view.mockPanelService.runBotAction).toHaveBeenCalledOnce());
    expect(view.mockMessageService.add).toHaveBeenCalledWith(
      expect.objectContaining({
        severity: 'warn',
        detail:
          'This bot is no longer ready to resume. Its custody state changed after this button was shown.',
      }),
    );
  });

  /**
   * `FleetDirectoryService.refresh()` had no caller before this fix, which is
   * the only reason the pre-freeze click-time fence read was harmless. Now
   * that the fence is frozen at open, a stale-generation refusal must refresh
   * the directory so the operator's next action is minted against a lane
   * they have actually been shown (#2068).
   */
  it('refreshes the directory after the coordinator refuses a stale generation', async () => {
    const directory = provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec' })],
    });
    const refresh = vi.spyOn(directory.useValue as never, 'refresh');
    const runBotAction = vi.fn().mockRejectedValue(
      new HttpErrorResponse({
        status: 409,
        error: { reason: 'clerk_binding_generation_conflict', message: 'Expected 3 is not 4.' },
      }),
    );
    const view = await renderPage([fakeCatalogBot()], {
      directory,
      runBotAction,
      panelActions: [fakePanelAction('stop')],
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));
    // `fireEvent` does not await the async output handler: flush the rejected
    // command and the directory refresh it schedules before asserting.
    await Promise.resolve();
    await Promise.resolve();

    expect(refresh).toHaveBeenCalledTimes(1);
    // `clerk_binding_generation_conflict` is a 409 in the fleet's closed
    // refusal vocabulary (#2067), so it now reads as a conflict, not the
    // generic "Unknown" `error` severity a pre-#2102 render gave every fleet
    // refusal.
    expect(view.mockMessageService.add).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'warn' }),
    );
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
    const runBotAction = vi.fn().mockRejectedValue(
      new HttpErrorResponse({
        status: 409,
        error: { reason: 'clerk_binding_generation_conflict', message: 'Expected 3 is not 4.' },
      }),
    );
    const view = await renderPage([fakeCatalogBot()], {
      directory,
      runBotAction,
      panelActions: [fakePanelAction('stop')],
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));
    // Flush both the rejected command and the refresh rejection handler.
    await Promise.resolve();
    await Promise.resolve();

    expect(view.mockMessageService.add).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'error', detail: LANE_FENCE_REFRESH_FAILED_MESSAGE }),
    );
    expect(runBotAction).toHaveBeenCalledTimes(1);
  });

  it('refuses a roster action whose lane rebound while the row was on screen', async () => {
    const directory = provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec' })],
    });
    const runBotAction = vi.fn().mockResolvedValue({ message: 'stopped' });
    const view = await renderPage([fakeCatalogBot()], {
      directory,
      runBotAction,
      panelActions: [fakePanelAction('stop')],
    });
    await screen.findByRole('button', { name: 'Stop' });

    // The operator is shown generation 3, then the coordinator rebinds to 4
    // before they press the button — exactly what refresh() will start doing.
    // The unfenced `target()`/`fleetScope()` reactively follows the rebind
    // (a read, not a command) and reloads the catalog; let that settle before
    // re-querying the button so the click lands on the current DOM node.
    directory.rebind({
      observed_at_ms: 1_757_000_000_001,
      clerks: [testLane({ clerk_id: 'clrk_spec', effective_binding_generation: 4 })],
    });
    // `rebind()` only stages the replacement; `refresh()` promotes it to
    // what `lanesOf()`/`lane()` report, like the real service's next load.
    await directory.useValue.refresh?.();
    await view.fixture.whenStable();
    view.fixture.detectChanges();

    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));

    expect(runBotAction).not.toHaveBeenCalled();
    expect(await screen.findByText(/rebound while the action was open/i)).toBeTruthy();
    expect(view.mockMessageService.add).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'warn' }),
    );
  });

  it('refuses a roster action when the lane had no known binding at open, and dispatches nothing', async () => {
    // Cold directory: the lane is present but its binding is unconfirmed, so
    // `openFence` freezes `{bindingGeneration: null, ...}` (#2068, decision
    // 15). A present-but-null lane, not an absent one: an absent lane would
    // also read as "drifted" by laneFenceDrifted, which would mask a deleted
    // enforceability branch behind the drift branch instead of proving it.
    const directory = provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec', effective_binding_generation: null })],
    });
    const runBotAction = vi.fn().mockResolvedValue({ message: 'stopped' });
    const view = await renderPage([fakeCatalogBot()], {
      directory,
      runBotAction,
      panelActions: [fakePanelAction('stop')],
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }));

    expect(runBotAction).not.toHaveBeenCalled();
    expect(await screen.findByText(/no known binding when the action was opened/i)).toBeTruthy();
    expect(view.mockMessageService.add).toHaveBeenCalledWith(
      expect.objectContaining({ severity: 'warn' }),
    );
  });
});
