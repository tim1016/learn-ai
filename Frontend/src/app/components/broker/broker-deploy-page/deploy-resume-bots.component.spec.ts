import { provideRouter } from '@angular/router';
import { fireEvent, render, screen } from '@testing-library/angular';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { resourceTarget } from '../../../fleet/resource-target';
import { fakeBotPanelView, fakeCatalogBot, fakePanelAction } from '../../../testing/bot-panel-fixtures';
import { BrokerV2PanelService } from '../v2-panel/lib/broker-v2-panel.service';
import type { BotPanelView, PanelActionResult } from '../v2-panel/lib/broker-v2-panel.types';
import { DeployResumeBotsComponent } from './deploy-resume-bots.component';

const TARGET = resourceTarget('alpaca', 'clrk_deploy', {
  accountId: 'PA9', bindingGeneration: 3, routingEpoch: 7,
});
const RESUMED_AT_MS = 1_790_000_000_000;

const STOPPED = fakeCatalogBot({
  strategy_instance_id: 'ema-spy',
  strategy_label: 'EMA Crossover',
  running: false,
  phase: 'OFF_DUTY',
  status_label: 'Off duty',
  status_explanation: 'Stopped by the operator.',
  row_action: fakePanelAction('resume'),
});

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

function panelWith(overrides: Partial<BotPanelView>): BotPanelView {
  return fakeBotPanelView({ strategy_instance_id: 'ema-spy', ...overrides });
}

async function renderSection(service: Partial<Record<keyof BrokerV2PanelService, unknown>>) {
  return render(DeployResumeBotsComponent, {
    inputs: { target: TARGET, accountId: 'PA9' },
    providers: [provideRouter([]), { provide: BrokerV2PanelService, useValue: service }],
  });
}

afterEach(() => vi.useRealTimers());

describe('DeployResumeBotsComponent (#2314)', () => {
  it('lists only the bots the backend offers a Resume for', async () => {
    const running = fakeCatalogBot({ strategy_instance_id: 'still-running', strategy_label: 'Running bot' });
    await renderSection({ getCatalog: vi.fn().mockResolvedValue([STOPPED, running]) });

    expect(await screen.findByText('EMA Crossover')).toBeTruthy();
    expect(screen.queryByText('Running bot')).toBeNull();
    expect(screen.getByRole('button', { name: 'Resume' })).toBeTruthy();
  });

  it('resumes through a frozen command and shows the window the warmup filled', async () => {
    vi.useFakeTimers();
    const runBotAction = vi.fn().mockResolvedValue(resumeResult());
    const getPanel = vi.fn().mockResolvedValue(
      panelWith({
        warmup_join: {
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
        },
      }),
    );
    const { fixture } = await renderSection({
      getCatalog: vi.fn().mockResolvedValue([STOPPED]),
      runBotAction,
      getPanel,
    });
    await vi.advanceTimersByTimeAsync(0);
    fixture.detectChanges();

    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    await vi.advanceTimersByTimeAsync(0);
    fixture.detectChanges();

    const [command, sid, action] = runBotAction.mock.calls[0];
    expect(sid).toBe('ema-spy');
    expect(action.action_id).toBe('resume');
    expect(command.entityId).toBe('ema-spy');
    expect(command.idempotencyKey).not.toBeNull();
    expect(screen.getByText('Bot resumed.')).toBeTruthy();

    await vi.advanceTimersByTimeAsync(2_000);
    fixture.detectChanges();

    expect(screen.getByText('Filled 173 missing bars from IBKR history')).toBeTruthy();
    expect(screen.getByText('Filled from')).toBeTruthy();
  });

  it('shows the refusal when the resumed run could not fill its gap', async () => {
    vi.useFakeTimers();
    const base = fakeBotPanelView();
    const refused = panelWith({
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
      warmup_join: {
        run_id: 'run-2',
        state: 'refused',
        label: 'Refused: gap could not be filled',
        explanation: 'IBKR history did not return every regular-hours minute.',
        retained_end_ms: RESUMED_AT_MS - 10_000_000,
        joined_at_ms: RESUMED_AT_MS + 1_000,
        filled_count: 0,
        filled_start_ms: null,
        filled_end_ms: null,
        warmed_from_history_only: false,
        reason_code: 'RESUME_HOLE_UNFILLED',
      },
    });
    const { fixture } = await renderSection({
      getCatalog: vi.fn().mockResolvedValue([STOPPED]),
      runBotAction: vi.fn().mockResolvedValue(resumeResult()),
      getPanel: vi.fn().mockResolvedValue(refused),
    });
    await vi.advanceTimersByTimeAsync(0);
    fixture.detectChanges();

    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    await vi.advanceTimersByTimeAsync(2_000);
    fixture.detectChanges();

    expect(screen.getByText('Refused: gap could not be filled')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Open bot control' }).getAttribute('href')).toBe(
      '/brokers/alpaca/clerks/clrk_deploy/accounts/PA9/bots/ema-spy',
    );
  });

  it('shows a refused Resume command without waiting on a warmup', async () => {
    const getPanel = vi.fn();
    const { fixture } = await renderSection({
      getCatalog: vi.fn().mockResolvedValue([STOPPED]),
      runBotAction: vi.fn().mockRejectedValue(new Error('Resume admission refused: market closed.')),
      getPanel,
    });
    await fixture.whenStable();
    fixture.detectChanges();

    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.getByRole('status').textContent).toContain('EMA Crossover');
    expect(getPanel).not.toHaveBeenCalled();
  });
});
