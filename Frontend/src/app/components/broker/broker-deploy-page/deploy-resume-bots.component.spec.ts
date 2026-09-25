import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { provideFleetDirectory, testLane } from '../../../fleet/fleet-directory-testing';
import type { LaneFence } from '../../../fleet/lane-fence';
import { resourceTarget } from '../../../fleet/resource-target';
import { fakeBotPanelView, fakeCatalogBot, fakePanelAction } from '../../../testing/bot-panel-fixtures';
import { BrokerV2PanelService } from '../v2-panel/lib/broker-v2-panel.service';
import type { BotPanelView, PanelActionResult } from '../v2-panel/lib/broker-v2-panel.types';
import { DeployResumeBotsComponent } from './deploy-resume-bots.component';

const TARGET = resourceTarget('alpaca', 'clrk_deploy', {
  accountId: 'PA9', bindingGeneration: 3, routingEpoch: 7,
});
const RESUMED_AT_MS = 1_790_000_000_000;
/** Frozen at desk render, deliberately different from TARGET's live stamp. */
const FENCE: LaneFence = { bindingGeneration: 2, routingEpoch: 6 };

type WarmupJoin = NonNullable<BotPanelView['warmup_join']>;
type StartupJoin = NonNullable<BotPanelView['startup_join']>;

/** A stopped roster row: like production, it carries no routine Resume of its own. */
const STOPPED = fakeCatalogBot({
  strategy_instance_id: 'ema-spy',
  strategy_label: 'EMA Crossover',
  running: false,
  phase: 'OFF_DUTY',
  status_label: 'Off duty',
  status_explanation: 'Stopped by the operator.',
  row_action: null,
});

function join(overrides: Partial<WarmupJoin>): WarmupJoin {
  return {
    run_id: 'run-2',
    state: 'filled',
    label: 'Filled 173 missing bars from IBKR history',
    explanation: 'The minutes that passed while the bot was stopped were fetched.',
    retained_end_ms: RESUMED_AT_MS - 10_000_000,
    joined_at_ms: RESUMED_AT_MS + 1_000,
    filled_count: 173,
    filled_start_ms: RESUMED_AT_MS - 10_000_000,
    filled_end_ms: RESUMED_AT_MS,
    warmed_from_history_only: false,
    reason_code: null,
    ...overrides,
  };
}

function startup(overrides: Partial<StartupJoin>): StartupJoin {
  return {
    run_id: 'run-2',
    state: 'ready',
    label: 'Ready: warmup met the live stream',
    explanation: 'Warmup and live bars are contiguous.',
    opened_at_ms: RESUMED_AT_MS + 100,
    live_from_ms: RESUMED_AT_MS + 60_000,
    joined_minute_start_ms: RESUMED_AT_MS,
    deadline_ms: RESUMED_AT_MS + 245_000,
    missing_start_ms: null,
    missing_end_ms: null,
    reason_code: null,
    ...overrides,
  };
}

/** The stopped bot's own panel, which is where its Resume is presented. */
function stoppedPanel(priorJoin: WarmupJoin | null = null): BotPanelView {
  return fakeBotPanelView({
    strategy_instance_id: 'ema-spy',
    actions: [fakePanelAction('resume')],
    warmup_join: priorJoin,
  });
}

function resumedPanel(overrides: Partial<BotPanelView>): BotPanelView {
  return fakeBotPanelView({ strategy_instance_id: 'ema-spy', ...overrides });
}

function resumeResult(): PanelActionResult {
  return {
    action_id: 'resume',
    outcome: 'success',
    receipt_id: 'receipt-1',
    recorded_at_ms: RESUMED_AT_MS,
    applied: true,
    revision: 2,
    concurrency_token: 'next',
    message: 'Bot resumed.',
  };
}

/**
 * A panel service whose reads answer with the stopped panel until Resume
 * succeeds and with `after` (a panel, or a read failure) from then on.
 */
function lane(after: () => Promise<BotPanelView>, before: BotPanelView = stoppedPanel()) {
  let resumed = false;
  return {
    getCatalog: vi.fn().mockResolvedValue([STOPPED]),
    runBotAction: vi.fn(async () => {
      resumed = true;
      return resumeResult();
    }),
    getPanel: vi.fn(() => (resumed ? after() : Promise.resolve(before))),
  };
}

async function renderSection(
  service: Partial<Record<keyof BrokerV2PanelService, unknown>>,
  fence: LaneFence = FENCE,
) {
  return render(DeployResumeBotsComponent, {
    inputs: { target: TARGET, accountId: 'PA9', fence },
    providers: [
      provideRouter([]),
      provideFleetDirectory({
        observed_at_ms: 1_757_000_000_000,
        clerks: [testLane({ clerk_id: 'clrk_deploy' })],
      }),
      { provide: BrokerV2PanelService, useValue: service },
    ],
  });
}

afterEach(() => vi.useRealTimers());

/** Fake timers need user-event to advance them, or its own delays never elapse. */
function user() {
  return vi.isFakeTimers() ? userEvent.setup({ advanceTimers: vi.advanceTimersByTime }) : userEvent.setup();
}

async function settle(fixture: { detectChanges(): void }, ms = 0): Promise<void> {
  await vi.advanceTimersByTimeAsync(ms);
  fixture.detectChanges();
}

describe('DeployResumeBotsComponent (#2314)', () => {
  it('lists each stopped bot with the Resume its own panel presents', async () => {
    const running = fakeCatalogBot({ strategy_instance_id: 'still-running', strategy_label: 'Running bot' });
    const service = lane(() => Promise.resolve(stoppedPanel()));
    service.getCatalog.mockResolvedValue([STOPPED, running]);
    await renderSection(service);

    expect(await screen.findByRole('button', { name: 'Resume' })).toBeTruthy();
    expect(screen.getByText('EMA Crossover')).toBeTruthy();
    expect(screen.queryByText('Running bot')).toBeNull();
    // Only the stopped bot's panel is read; a running bot offers no Resume.
    expect(service.getPanel).toHaveBeenCalledTimes(1);
  });

  it('resumes through the frozen fence and shows the window the warmup filled', async () => {
    vi.useFakeTimers();
    const service = lane(() =>
      Promise.resolve(resumedPanel({ warmup_join: join({}), startup_join: startup({}) })),
    );
    const { fixture } = await renderSection(service);
    await settle(fixture);

    await user().click(screen.getByRole('button', { name: 'Resume' }));
    await settle(fixture);

    const [command, sid, action] = service.runBotAction.mock.calls[0] as unknown as [
      { entityId: string; idempotencyKey: string | null; bindingGeneration: number; routingEpoch: number },
      string,
      { action_id: string },
    ];
    expect(sid).toBe('ema-spy');
    expect(action.action_id).toBe('resume');
    expect(command.entityId).toBe('ema-spy');
    expect(command.idempotencyKey).not.toBeNull();
    // Minted from the desk's frozen fence, never the live target's stamp (#2106).
    expect([command.bindingGeneration, command.routingEpoch]).toEqual([2, 6]);
    expect(screen.getByText('Bot resumed.')).toBeTruthy();

    await settle(fixture, 2_000);

    expect(screen.getByText('Filled 173 missing bars from IBKR history')).toBeTruthy();
    expect(screen.getByText('Filled from')).toBeTruthy();
  });

  it('ignores the stopped run’s own join handed back by a read begun before Resume', async () => {
    vi.useFakeTimers();
    const prior = join({ run_id: 'run-1', label: 'Warmed on its retained bars', state: 'contiguous' });
    const stale = resumedPanel({ warmup_join: prior });
    const fresh = resumedPanel({ warmup_join: join({ run_id: 'run-2' }), startup_join: startup({}) });
    // The list reload after Resume and the first warmup poll both read the stale panel.
    let reads = 0;
    const service = lane(() => Promise.resolve(reads++ < 2 ? stale : fresh), stoppedPanel(prior));
    const { fixture } = await renderSection(service);
    await settle(fixture);

    await user().click(screen.getByRole('button', { name: 'Resume' }));
    await settle(fixture, 2_000);
    expect(screen.queryByText('Warmed on its retained bars')).toBeNull();

    await settle(fixture, 2_000);
    expect(screen.getByText('Filled 173 missing bars from IBKR history')).toBeTruthy();
  });

  it('shows the refusal when the resumed run could not fill its gap', async () => {
    vi.useFakeTimers();
    const base = fakeBotPanelView();
    const refused = resumedPanel({
      health: {
        ...base.health,
        running: false,
        duty_outcome: {
          kind: 'CRASHED',
          reason_code: 'RESUME_HOLE_UNFILLED',
          label: 'Refused: gap could not be filled',
          explanation: 'IBKR history did not return every regular-hours minute.',
          recorded_at_ms: RESUMED_AT_MS + 3_000,
          run_id: 'run-2',
        },
      },
    });
    const { fixture } = await renderSection(lane(() => Promise.resolve(refused)));
    await settle(fixture);

    await user().click(screen.getByRole('button', { name: 'Resume' }));
    await settle(fixture, 2_000);

    expect(screen.getByText('Refused: gap could not be filled')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Open bot control' }).getAttribute('href')).toBe(
      '/brokers/alpaca/clerks/clrk_deploy/accounts/PA9/bots/ema-spy',
    );
  });

  it('shows the resumed run preparing, with the time remaining before refusal, until it is ready', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(RESUMED_AT_MS + 65_000);
    let reads = 0;
    const filling = resumedPanel({
      startup_join: startup({
        state: 'filling',
        label: 'Preparing: filling the minutes before the live stream from IBKR history',
      }),
    });
    const ready = resumedPanel({ startup_join: startup({}) });
    const service = lane(() => Promise.resolve(reads++ < 2 ? filling : ready));
    const { fixture } = await renderSection(service);
    await settle(fixture);

    await user().click(screen.getByRole('button', { name: 'Resume' }));
    await settle(fixture, 2_000);

    expect(screen.getByText(/Preparing: filling the minutes/)).toBeTruthy();
    expect(screen.getByText('Time remaining before refusal')).toBeTruthy();
    // 245 s after the resume less the 67 s now elapsed.
    expect(screen.getByText('2:58')).toBeTruthy();

    await settle(fixture, 2_000);
    expect(screen.getByText('Ready: warmup met the live stream')).toBeTruthy();
    expect(screen.queryByText('Time remaining before refusal')).toBeNull();
  });

  it('says a refused resume left a position it is not managing', async () => {
    vi.useFakeTimers();
    const base = fakeBotPanelView();
    const refused = resumedPanel({
      startup_join: startup({ state: 'refused', label: 'Refused: warmup history unavailable' }),
      health: {
        ...base.health,
        running: false,
        duty_outcome: {
          kind: 'CRASHED',
          reason_code: 'WARMUP_HISTORY_UNAVAILABLE',
          label: 'Refused: warmup history unavailable',
          explanation: 'IB Gateway did not return the warmup history the run needs.',
          recorded_at_ms: RESUMED_AT_MS + 200_000,
          run_id: 'run-2',
          exposure_notices: [
            {
              kind: 'position_unmanaged',
              label: 'Bot is not managing this position',
              explanation: 'The Clerk attributes 3 SPY to this bot.',
            },
          ],
        },
      },
    });
    const { fixture } = await renderSection(lane(() => Promise.resolve(refused)));
    await settle(fixture);

    await user().click(screen.getByRole('button', { name: 'Resume' }));
    await settle(fixture, 2_000);

    expect(screen.getByRole('alert').textContent).toContain('Bot is not managing this position');
    expect(screen.getByText('The Clerk attributes 3 SPY to this bot.')).toBeTruthy();
  });

  it('shows a refused Resume command without following a warmup', async () => {
    const service = lane(() => Promise.resolve(stoppedPanel()));
    service.runBotAction.mockRejectedValue(new Error('Resume admission refused: market closed.'));
    const { fixture } = await renderSection(service);
    await fixture.whenStable();
    fixture.detectChanges();

    await user().click(await screen.findByRole('button', { name: 'Resume' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByRole('status').textContent).toContain('EMA Crossover');
    expect(screen.queryByText(/Warming up/)).toBeNull();
  });

  it('refuses to mint a Resume when the desk fence cannot be enforced', async () => {
    const service = lane(() => Promise.resolve(stoppedPanel()));
    const { fixture } = await renderSection(service, { bindingGeneration: null, routingEpoch: null });
    await fixture.whenStable();
    fixture.detectChanges();

    await user().click(await screen.findByRole('button', { name: 'Resume' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(service.runBotAction).not.toHaveBeenCalled();
    expect(screen.getByRole('status')).toBeTruthy();
  });

  it('says it could not read the bot when every re-read fails, not that the bot is silent', async () => {
    vi.useFakeTimers();
    const { fixture } = await renderSection(lane(() => Promise.reject(new Error('panel read failed'))));
    await settle(fixture);

    await user().click(screen.getByRole('button', { name: 'Resume' }));
    await settle(fixture, 2_000 * 150);

    expect(screen.getByText(/could not read the bot after Resume/)).toBeTruthy();
  });
});
