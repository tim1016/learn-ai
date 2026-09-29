import { fireEvent, render, screen, within } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { fakeBotPanelView, fakePanelAction } from '../../../../testing/bot-panel-fixtures';
import { formatTimestampDisplay } from '../../../../shared/timestamp/timestamp-display';
import {
  AlpacaLiveVerdictService,
  LANE_MODE_WORDING,
  type LaneVerdictState,
} from '../../../../services/alpaca-live-verdict.service';
import type { BotPanelView, BotRunView, CurrentRunState } from '../lib/broker-v2-panel.types';
import { EMPTY_CURRENT_RUN_STATE } from '../lib/broker-v2-panel.types';
import { BotBannerComponent } from './bot-banner.component';

const BACK_ROUTE = ['brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'pa9', 'bots'];

function fakeRun(overrides: Partial<BotRunView> = {}): BotRunView {
  return {
    strategy_instance_id: 'spy-momentum-01',
    run_id: 'run-current',
    configuration_hash: 'a'.repeat(64),
    launch_reason: 'deploy',
    started_at_ms: 1_753_800_000_000,
    is_current: true,
    process: {
      strategy_instance_id: 'spy-momentum-01',
      run_id: 'run-current',
      process_identity: 'process-1',
      state: 'RUNNING',
      registry_generation: 'registry-1',
      observed_at_ms: 1_753_800_005_000,
    },
    terminal_outcome: null,
    ...overrides,
  };
}

const PAPER_VERDICT = { verdict: { final_verdict: 'paper' }, lastError: null } as unknown as LaneVerdictState;
const verdicts = { stateFor: vi.fn(() => PAPER_VERDICT) };

function stopped(overrides: Partial<BotPanelView> = {}): Partial<BotPanelView> {
  const panel = fakeBotPanelView();
  return { health: { ...panel.health, running: false, desired_state: 'STOPPED' }, actions: [], primary_action: null, ...overrides };
}

async function renderBanner(
  overrides: Partial<BotPanelView> = {},
  runState: CurrentRunState = EMPTY_CURRENT_RUN_STATE,
  on: Record<string, (event: unknown) => void> = {},
) {
  return render(BotBannerComponent, {
    inputs: {
      panel: fakeBotPanelView(overrides),
      runState,
      backRoute: BACK_ROUTE,
      backLabel: 'Bots',
      clerkId: 'clrk_spec',
      routeAccountId: 'pa9',
    },
    providers: [provideRouter([]), { provide: AlpacaLiveVerdictService, useValue: verdicts }],
    on,
  });
}

describe('BotBannerComponent', () => {
  it('names the bot by its id, with its strategy, symbol, the way back and its freshness', async () => {
    await renderBanner();

    expect(screen.getByRole('link', { name: 'Bots' }).getAttribute('href'))
      .toBe('/brokers/alpaca/clerks/clrk_spec/accounts/pa9/bots');
    expect(screen.getByRole('heading', { level: 2, name: 'spy-momentum-01' })).toBeTruthy();
    expect(screen.getByText('Deployment Validation')).toBeTruthy();
    expect(screen.getByText('SPY')).toBeTruthy();
    expect(screen.getByText(/Updated/)).toBeTruthy();
    expect(screen.getByRole('status', { name: 'Revision 1 running' }).textContent).toBe('Revision 1 running');
  });

  it('shows the state once in the topline and recolours it for an off-duty bot', async () => {
    const { container } = await renderBanner({
      mission_verdict: {
        state: 'off_duty',
        label: 'Off duty',
        explanation: 'The runtime is not scheduled to run right now.',
        next_action: null,
        evaluated_at_ms: 1_753_800_000_000,
      },
    });

    const topline = container.querySelector('.bot-banner__topline') as HTMLElement;
    expect(topline.getAttribute('data-state')).toBe('off_duty');
    expect(within(topline).getByRole('status', { name: 'Off duty' })).toBeTruthy();
    expect(screen.getAllByRole('status', { name: 'Off duty' })).toHaveLength(1);
  });

  it('names the lane world for a bot that trades the account money', async () => {
    await renderBanner();

    expect(screen.getByText(LANE_MODE_WORDING.paper)).toBeTruthy();
    expect(screen.queryByText(/DRY RUN/)).toBeNull();
    expect(verdicts.stateFor).toHaveBeenCalledWith('clrk_spec');
  });

  it('marks a Dry Run bot as simulated cash, never with the lane world (H23)', async () => {
    await renderBanner({ mode: 'dry_run' });

    expect(screen.getByText('DRY RUN · simulated cash')).toBeTruthy();
    expect(screen.queryByText(LANE_MODE_WORDING.paper)).toBeNull();
    expect(screen.queryByText(LANE_MODE_WORDING.live)).toBeNull();
  });

  it('renders the backend primary action for a running bot and no Deploy again', async () => {
    const requested: unknown[] = [];
    await renderBanner(
      { actions: [fakePanelAction('stop')], primary_action: 'stop' },
      EMPTY_CURRENT_RUN_STATE,
      { actionRequested: (event) => requested.push(event) },
    );

    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    expect(requested).toEqual([{ action: fakePanelAction('stop'), reason: null }]);
    expect(screen.queryByRole('link', { name: 'Deploy again' })).toBeNull();
  });

  it('renders no primary action when the backend names one it did not present', async () => {
    await renderBanner({ actions: [], primary_action: 'stop' });

    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
  });

  it('offers Deploy again for a stopped bot, carrying the bot it came from, and sends nothing', async () => {
    const requested: unknown[] = [];
    await renderBanner(stopped(), EMPTY_CURRENT_RUN_STATE, { actionRequested: (event) => requested.push(event) });

    expect(screen.getByRole('link', { name: 'Deploy again' }).getAttribute('href'))
      .toBe('/brokers/alpaca/clerks/clrk_spec/accounts/pa9/deploy?from=spy-momentum-01');
    expect(requested).toEqual([]);
  });

  it('opens the manual ticket under the routed account', async () => {
    await renderBanner();

    fireEvent.click(screen.getByRole('button', { name: 'More actions for this bot' }));
    const manual = screen.getByRole('link', { name: 'Manual order' });
    expect(manual.getAttribute('href')).toMatch(/^\/brokers\/alpaca\/clerks\/clrk_spec\/accounts\/pa9\?/);
    expect(manual.getAttribute('href')).toContain('accountId=PA9');
  });

  it('never offers Retire or Archive, even when the backend presents them armed (owner decision 2026-09-28)', async () => {
    // Clearing a finished bot is Home's Finished fold alone; the backend still
    // presents both actions because the bulk clear reads archive's token.
    await renderBanner(stopped({
      actions: [fakePanelAction('retire', { label: 'Retire' }), fakePanelAction('archive', { label: 'Archive' })],
    }));

    fireEvent.click(screen.getByRole('button', { name: 'More actions for this bot' }));
    expect(screen.getByRole('link', { name: 'Manual order' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Retire/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /Archive/ })).toBeNull();
  });

  it("renders a cleared bot's page read-only, with Deploy again and the way to History (#2574)", async () => {
    const panel = fakeBotPanelView();
    await renderBanner(stopped({ health: { ...panel.health, running: false, desired_state: 'STOPPED', phase: 'RETIRED' } }));

    expect(screen.getByRole('note').textContent).toContain('its records here are read-only');
    expect(screen.getByRole('link', { name: 'Every cleared bot is in History' }).getAttribute('href'))
      .toBe('/brokers/alpaca/clerks/clrk_spec/history?status=cleared');
    expect(screen.getByRole('link', { name: 'Deploy again' }).getAttribute('href'))
      .toBe('/brokers/alpaca/clerks/clrk_spec/accounts/pa9/deploy?from=spy-momentum-01');
    expect(screen.queryByRole('button', { name: 'More actions for this bot' })).toBeNull();
  });

  it('keeps the cure on a retired bot that still holds shares: it is not cleared', async () => {
    const panel = fakeBotPanelView();
    await renderBanner(stopped({
      health: { ...panel.health, running: false, desired_state: 'STOPPED', phase: 'RETIRED' },
      actions: [fakePanelAction('flatten_stop', { label: 'Flatten' })],
      primary_action: 'flatten_stop',
    }));

    expect(screen.queryByRole('note')).toBeNull();
    expect(screen.getByRole('button', { name: /Flatten/ })).toBeTruthy();
  });

  it('offers a Dry Run bot no manual ticket on the account', async () => {
    await renderBanner({ mode: 'dry_run' });

    expect(screen.queryByRole('button', { name: 'More actions for this bot' })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Manual order' })).toBeNull();
  });

  it('shows the run timing and the strategy clocks', async () => {
    await renderBanner({}, {
      run: fakeRun({
        terminal_outcome: { kind: 'STOPPED', reason_code: 'OPERATOR_STOP', recorded_at_ms: 1_753_850_000_000, run_id: 'run-current' },
      }),
      loading: false,
      failed: false,
    });

    expect(screen.getByText(formatTimestampDisplay(1_753_800_000_000, { granularity: 'time' }))).toBeTruthy();
    expect(screen.getByText(formatTimestampDisplay(1_753_850_000_000, { granularity: 'time' }))).toBeTruthy();
    expect(screen.getByText('Last decision')).toBeTruthy();
  });

  it('surfaces a run-timing retry request', async () => {
    const retried = { called: false };
    await renderBanner({}, { run: null, loading: false, failed: true }, { retryRequested: () => { retried.called = true; } });

    fireEvent.click(screen.getByRole('button', { name: 'Retry run timing' }));
    expect(retried.called).toBe(true);
  });

  it.each([
    ['running', { actions: [fakePanelAction('stop'), fakePanelAction('retire', { label: 'Retire' })], primary_action: 'stop' as const }],
    ['stopped Dry Run', stopped({ mode: 'dry_run' })],
  ])('has no detectable accessibility violations (%s)', async (_name, overrides) => {
    await renderBanner(overrides);

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations).toEqual([]);
  });
});
