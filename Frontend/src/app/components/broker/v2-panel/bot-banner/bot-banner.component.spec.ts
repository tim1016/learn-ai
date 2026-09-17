import { fireEvent, render, screen } from '@testing-library/angular';
import { provideRouter } from '@angular/router';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { fakeBotPanelView, fakePanelAction } from '../../../../testing/bot-panel-fixtures';
import type { BotPanelView, PanelAction } from '../lib/broker-v2-panel.types';
import { EMPTY_CURRENT_RUN_STATE } from '../lib/broker-v2-panel.types';
import { BotBannerComponent } from './bot-banner.component';

const BACK_ROUTE = ['brokers', 'alpaca', 'clerks', 'clrk_spec', 'accounts', 'acc-1', 'bots'];

function inputs(overrides: Partial<BotPanelView> = {}, operator = false) {
  return {
    panel: fakeBotPanelView(overrides),
    runState: EMPTY_CURRENT_RUN_STATE,
    backRoute: BACK_ROUTE,
    backLabel: 'Bots',
    clerkId: 'clrk_spec',
    operator,
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
    expect(screen.getByRole('region', { name: 'Latest run timing' })).toBeTruthy();
    expect(screen.getByText(/Updated/)).toBeTruthy();
    const revisionStatus = screen.getByRole('status', { name: 'Revision 1 running' });
    expect(revisionStatus.textContent).toBe('Revision 1 running');
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
