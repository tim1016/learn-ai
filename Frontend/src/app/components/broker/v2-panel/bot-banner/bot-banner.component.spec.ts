import { fireEvent, render, screen, within } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { fakeBotPanelView, fakePanelAction } from '../../../../testing/bot-panel-fixtures';
import { formatTimestampDisplay } from '../../../../shared/timestamp/timestamp-display';
import type { BotPanelView, BotRunView, CurrentRunState, PanelAction } from '../lib/broker-v2-panel.types';
import { EMPTY_CURRENT_RUN_STATE } from '../lib/broker-v2-panel.types';
import { BotBannerComponent } from './bot-banner.component';

const BACK_ROUTE = ['brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'acc-1', 'bots'];

function fakeRun(overrides: Partial<BotRunView> = {}): BotRunView {
  return {
    strategy_instance_id: 'sid-001',
    run_id: 'run-current',
    configuration_hash: 'a'.repeat(64),
    launch_reason: 'deploy',
    started_at_ms: 1_753_800_000_000,
    is_current: true,
    process: {
      strategy_instance_id: 'sid-001',
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

function inputs(overrides: Partial<BotPanelView> = {}, operator = false, runState: CurrentRunState = EMPTY_CURRENT_RUN_STATE) {
  return {
    panel: fakeBotPanelView(overrides),
    runState,
    backRoute: BACK_ROUTE,
    backLabel: 'Bots',
    clerkId: 'clrk_spec',
    lens: operator ? 'operator' as const : 'trader' as const,
  };
}

describe('BotBannerComponent', () => {
  it('shows which bot this is, the way back, and its freshness once, above the switch', async () => {
    await render(BotBannerComponent, {
      inputs: inputs(),
      providers: [provideRouter([])],
    });

    const back = screen.getByRole('link', { name: 'Bots' });
    expect(back.getAttribute('href')).toBe('/brokers/alpaca/clerks/clrk_spec/accounts/acc-1/bots');
    expect(screen.getByRole('heading', { name: /Deployment Validation/ })).toBeTruthy();
    expect(screen.getByText('SPY')).toBeTruthy();
    expect(screen.getByText('spy-momentum-01')).toBeTruthy();
    expect(screen.getByText(/Updated/)).toBeTruthy();
    const revisionStatus = screen.getByRole('status', { name: 'Revision 1 running' });
    expect(revisionStatus.textContent).toBe('Revision 1 running');
  });

  it('shows the working/off-duty state once, in the topline next to the back link', async () => {
    const { container } = await render(BotBannerComponent, {
      inputs: inputs(),
      providers: [provideRouter([])],
    });

    const topline = container.querySelector('.bot-banner__topline');
    expect(topline?.getAttribute('data-state')).toBe('working');
    const status = within(topline as HTMLElement).getByRole('status', { name: 'Working' });
    expect(status.textContent).toBe('Working');
    // The status-row no longer carries a second copy of the same chip.
    expect(screen.getAllByRole('status', { name: 'Working' })).toHaveLength(1);
  });

  it('recolors the topline for an off-duty bot', async () => {
    const { container } = await render(BotBannerComponent, {
      inputs: inputs({
        mission_verdict: {
          state: 'off_duty',
          label: 'Off duty',
          explanation: 'The runtime is not scheduled to run right now.',
          next_action: null,
          evaluated_at_ms: 1_753_800_000_000,
        },
      }),
      providers: [provideRouter([])],
    });

    const topline = container.querySelector('.bot-banner__topline');
    expect(topline?.getAttribute('data-state')).toBe('off_duty');
    expect(screen.getByRole('status', { name: 'Off duty' })).toBeTruthy();
  });

  it('shows the current run\'s Started/Ended times, compactly, in the topline', async () => {
    await render(BotBannerComponent, {
      inputs: inputs({}, false, {
        run: fakeRun({
          terminal_outcome: {
            kind: 'STOPPED',
            reason_code: 'OPERATOR_STOP',
            recorded_at_ms: 1_753_850_000_000,
            run_id: 'run-current',
          },
        }),
        loading: false,
        failed: false,
      }),
      providers: [provideRouter([])],
    });

    expect(screen.getByText(
      formatTimestampDisplay(1_753_800_000_000, { granularity: 'time' }),
    )).toBeTruthy();
    expect(screen.getByText(
      formatTimestampDisplay(1_753_850_000_000, { granularity: 'time' }),
    )).toBeTruthy();
  });

  it('shows a still-running run as Running, not a blank end time', async () => {
    await render(BotBannerComponent, {
      inputs: inputs({}, false, { run: fakeRun(), loading: false, failed: false }),
      providers: [provideRouter([])],
    });

    expect(screen.getByText('Running')).toBeTruthy();
  });

  it('surfaces a run-timing retry request from the caller', async () => {
    const retried = { called: false };
    await render(BotBannerComponent, {
      inputs: inputs({}, false, { run: null, loading: false, failed: true }),
      providers: [provideRouter([])],
      on: { retryRequested: () => { retried.called = true; } },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Retry run timing' }));
    expect(retried.called).toBe(true);
  });

  it('shows loading and unavailable placeholders while the run has not resolved', async () => {
    const { rerender } = await render(BotBannerComponent, {
      inputs: inputs({}, false, { run: null, loading: true, failed: false }),
      providers: [provideRouter([])],
    });

    expect(screen.getAllByText('Loading…')).toHaveLength(2);

    await rerender({ inputs: inputs({}, false, { run: null, loading: false, failed: true }) });

    expect(screen.getAllByText('Unavailable')).toHaveLength(2);
  });

  it('has no detectable accessibility violations in either lens', async () => {
    await render(BotBannerComponent, {
      inputs: {
        ...inputs(
          {
            actions: [fakePanelAction('stop'), fakePanelAction('retire', { label: 'Retire' })],
            primary_action_by_lens: { trader: 'stop', operator: 'stop' },
          },
          true,
        ),
        tickerQuote: { ticker: 'SPY', price: 512.3, changePercent: 0.6 },
      },
      providers: [provideRouter([])],
    });

    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false } },
    });
    expect(results.violations).toEqual([]);
  });

  it('puts the strategy title and its actions on one row, not two', async () => {
    const { container } = await render(BotBannerComponent, {
      inputs: inputs({
        actions: [fakePanelAction('resume')],
        primary_action_by_lens: { trader: 'resume', operator: 'resume' },
      }),
      providers: [provideRouter([])],
    });

    const row = container.querySelector('.bot-banner__status-row');
    const heading = screen.getByRole('heading', { name: /Deployment Validation/ });
    const button = screen.getByRole('button', { name: 'Resume' });
    expect(row?.contains(heading)).toBe(true);
    expect(row?.contains(button)).toBe(true);
  });

  it('shows the trader mission verdict and direct trader actions, with no quote or promoted flatten', async () => {
    await render(BotBannerComponent, {
      inputs: inputs({
        actions: [fakePanelAction('resume')],
        primary_action_by_lens: { trader: 'resume', operator: 'resume' },
      }),
      providers: [provideRouter([])],
    });

    expect(screen.getByRole('status', { name: 'Working' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Resume' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'More trader actions' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'More operator actions' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'More trader actions' }));
    expect(screen.getByRole('link', { name: 'Manual order' })).toBeTruthy();
  });

  it('never renders an Operator-only action as the trader primary command, even when presented (#1665)', async () => {
    await render(BotBannerComponent, {
      inputs: inputs({
        actions: [
          fakePanelAction('resume'),
          fakePanelAction('resolve_execution_coverage', { label: 'Resolve execution coverage' }),
        ],
        primary_action_by_lens: { trader: null, operator: 'resolve_execution_coverage' },
      }),
      providers: [provideRouter([])],
    });

    expect(screen.queryByRole('button', { name: 'Resolve execution coverage' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull();
  });

  it('combines the operator quote with direct lifecycle and recovery actions', async () => {
    const { container } = await render(BotBannerComponent, {
      inputs: {
        ...inputs(
          {
            actions: [fakePanelAction('stop'), fakePanelAction('retire', { label: 'Retire' })],
            primary_action_by_lens: { trader: 'stop', operator: 'stop' },
          },
          true,
        ),
        tickerQuote: { ticker: 'SPY', price: 512.3, changePercent: 0.6 },
      },
      providers: [provideRouter([])],
    });

    expect(container.querySelector('app-panel-instrument-quote')).not.toBeNull();
    expect(screen.getByRole('button', { name: 'Stop' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'More operator actions' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'More operator actions' }));
    expect(screen.getByRole('button', { name: 'Retire' })).toBeTruthy();
  });

  const prepareFlatten: PanelAction = fakePanelAction('prepare_safe_flatten', {
    label: 'Prepare safe flatten',
  });
  const executeFlatten: PanelAction = fakePanelAction('execute_safe_flatten', {
    label: 'Execute safe flatten',
  });
  const resume: PanelAction = fakePanelAction('resume');

  function stoppedPanel(overrides: Partial<BotPanelView> = {}): Partial<BotPanelView> {
    return {
      health: { ...fakeBotPanelView().health, running: false, phase: 'OFF_DUTY', desired_state: 'STOPPED' },
      actions: [resume, prepareFlatten],
      primary_action_by_lens: { trader: null, operator: 'resume' },
      ...overrides,
    };
  }

  it('promotes flatten beside Resume for a stopped bot still holding exposure, operator only', async () => {
    await render(BotBannerComponent, {
      inputs: inputs(stoppedPanel({ exposure: { SPY: 3 } }), true),
      providers: [provideRouter([])],
    });

    expect(screen.getByRole('button', { name: 'Resume' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Prepare safe flatten' })).toBeTruthy();
  });

  it('prefers the execute step once the backend presents it', async () => {
    await render(BotBannerComponent, {
      inputs: inputs(
        stoppedPanel({ exposure: { SPY: 3 }, actions: [resume, prepareFlatten, executeFlatten] }),
        true,
      ),
      providers: [provideRouter([])],
    });

    expect(screen.getByRole('button', { name: 'Execute safe flatten' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Prepare safe flatten' })).toBeNull();
  });

  it('promotes Prepare when the backend presents Execute disabled (#2007)', async () => {
    // Outside the regular session the unpriced Execute is blocked and Prepare
    // is where the limit is priced; promoting the dead button would bury it.
    await render(BotBannerComponent, {
      inputs: inputs(
        stoppedPanel({
          exposure: { SPY: 3 },
          actions: [
            resume,
            prepareFlatten,
            { ...executeFlatten, enabled: false },
          ],
        }),
        true,
      ),
      providers: [provideRouter([])],
    });

    expect(screen.getByRole('button', { name: 'Prepare safe flatten' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'More operator actions' }));
    expect(screen.getByRole('button', { name: /Execute safe flatten/ })).toBeTruthy();
  });

  it('does not promote flatten for a stopped bot that is already flat', async () => {
    await render(BotBannerComponent, {
      inputs: inputs(stoppedPanel({ exposure: { SPY: 0 } }), true),
      providers: [provideRouter([])],
    });

    expect(screen.getByRole('button', { name: 'Resume' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Prepare safe flatten' })).toBeNull();
  });

  it('does not promote flatten while the bot is still running', async () => {
    await render(BotBannerComponent, {
      inputs: inputs(
        { exposure: { SPY: 3 }, actions: [prepareFlatten], primary_action_by_lens: { trader: null, operator: null } },
        true,
      ),
      providers: [provideRouter([])],
    });

    expect(screen.queryByRole('button', { name: 'Prepare safe flatten' })).toBeNull();
  });

  it('never promotes flatten or a live quote for the trader lens', async () => {
    const { container } = await render(BotBannerComponent, {
      inputs: {
        ...inputs(stoppedPanel({ exposure: { SPY: 3 } }), false),
        tickerQuote: { ticker: 'SPY', price: 512.3, changePercent: 0.6 },
      },
      providers: [provideRouter([])],
    });

    expect(screen.queryByRole('button', { name: 'Prepare safe flatten' })).toBeNull();
    expect(container.querySelector('app-panel-instrument-quote')).toBeNull();
  });
});
