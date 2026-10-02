/** #2794 R2, R6: the bot page's toolbar renders the backend's action list --
 * offered actions as icon buttons, blocked ones disabled with their reason,
 * not-needed ones only in All actions. */
import { provideRouter } from '@angular/router';
import { render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { fakeBotPage, fakeBotPanelView, fakePanelAction, fakeSqliteStopAction } from '../../../../testing/bot-panel-fixtures';
import type { BotPanelView, ToolbarActionView } from '../lib/broker-v2-panel.types';
import { BotToolbarComponent } from './bot-toolbar.component';

const DEPLOY_AGAIN = { commands: ['/brokers', 'alpaca', 'deploy'], queryParams: { from: 'spy-momentum-01' } };

function toolbarEntry(entry: Partial<ToolbarActionView> & Pick<ToolbarActionView, 'action_id' | 'label'>): ToolbarActionView {
  return { group: 'bot', availability: 'available', reason: `${entry.label}.`, tone: 'neutral', primary: false, ...entry };
}

/** A running bot whose backend list offers Stop and Check against Alpaca, blocks Cancel, and needs no Sell. */
function runningPanel(): BotPanelView {
  const panel = fakeBotPanelView({
    actions: [
      fakeSqliteStopAction(),
      fakePanelAction('reconcile_now', { label: 'Reconcile now' }),
      fakePanelAction('cancel_verified_working_orders', { label: 'Cancel verified working orders', enabled: false }),
      fakePanelAction('prepare_safe_flatten', { label: 'Prepare safe flatten', enabled: false, needed: false }),
    ],
  });
  return {
    ...panel,
    bot_page: {
      ...fakeBotPage(panel),
      toolbar: [
        toolbarEntry({ action_id: 'stop_bot_decisions', label: 'Stop', tone: 'danger', primary: true }),
        toolbarEntry({ action_id: 'prepare_safe_flatten', label: 'Sell', availability: 'not_needed', reason: 'This bot holds no shares.' }),
        toolbarEntry({ action_id: 'deploy_again', label: 'Deploy again', availability: 'not_needed', reason: 'This bot is still running.' }),
        toolbarEntry({ action_id: 'reconcile_now', label: 'Check against Alpaca', group: 'fix' }),
        toolbarEntry({
          action_id: 'cancel_verified_working_orders', label: 'Cancel open orders', group: 'fix', availability: 'blocked',
          reason: 'No working order has both a durable Clerk reference and broker identity.',
        }),
        toolbarEntry({ action_id: 'build_proof', label: 'Build proof', group: 'inspect' }),
      ],
    },
  };
}

async function renderToolbar(panel: BotPanelView) {
  const actionRequested = vi.fn();
  const buildProof = vi.fn();
  const view = await render(BotToolbarComponent, {
    inputs: { panel, deployAgain: DEPLOY_AGAIN, manualOrder: null },
    on: { actionRequested, buildProof },
    providers: [provideRouter([])],
  });
  return { ...view, actionRequested, buildProof };
}

describe('BotToolbarComponent (#2794)', () => {
  afterEach(() => localStorage.clear());

  it('groups the offered actions, fills the primary in its own tone and keeps Stop named; not-needed ones stay out', async () => {
    await renderToolbar(runningPanel());

    const toolbar = screen.getByRole('toolbar', { name: 'Actions for this bot' });
    expect(within(toolbar).getAllByRole('group').map((group) => group.getAttribute('aria-label')))
      .toEqual(['Bot', 'Fix', 'Inspect']);
    const stop = within(toolbar).getByRole('button', { name: 'Stop' });
    expect(stop.textContent?.trim()).toBe('Stop');
    // Thermo on #2794: the primary is filled, and a filled Stop stays a danger action.
    expect(stop.className).toContain('panel-action__button--filled');
    expect(stop.className).toContain('panel-action__button--danger');
    // An icon button keeps its name for screen readers and its reason as a tooltip.
    const check = within(toolbar).getByRole('button', { name: 'Check against Alpaca' });
    expect(check.textContent?.trim()).toBe('');
    expect(within(toolbar).queryByRole('button', { name: 'Sell' })).toBeNull();
    expect(within(toolbar).queryByRole('link', { name: 'Deploy again' })).toBeNull();
  });

  it('shows a needed action it blocks disabled, with the backend’s reason', async () => {
    await renderToolbar(runningPanel());

    const cancel = screen.getByRole('button', { name: 'Cancel open orders' });
    expect(cancel.getAttribute('aria-disabled')).toBe('true');
    expect(cancel.getAttribute('title')).toContain('No working order has both a durable Clerk reference and broker identity.');
  });

  it('runs an offered custody action through the presented action, and opens Build proof', async () => {
    const user = userEvent.setup();
    const { actionRequested, buildProof } = await renderToolbar(runningPanel());

    await user.click(screen.getByRole('button', { name: 'Check against Alpaca' }));
    await user.click(screen.getByRole('button', { name: 'Build proof' }));

    expect(actionRequested).toHaveBeenCalledWith(expect.objectContaining({
      action: expect.objectContaining({ action_id: 'reconcile_now' }),
    }));
    expect(buildProof).toHaveBeenCalledTimes(1);
  });

  it('lists every action in All actions -- available, blocked or not needed -- with its reason and system name', async () => {
    const user = userEvent.setup();
    await renderToolbar(runningPanel());

    await user.click(screen.getByRole('button', { name: 'All actions' }));

    const list = screen.getByRole('region', { name: 'All actions for this bot' });
    const sell = within(list).getAllByRole('listitem').find((item) => item.textContent?.includes('prepare_safe_flatten'));
    for (const words of ['Sell', 'prepare_safe_flatten', 'This bot holds no shares.', 'Not needed']) {
      expect(sell?.textContent).toContain(words);
    }
    expect(within(list).getAllByRole('listitem')).toHaveLength(6);
  });

  it('names every icon once Labels is on, and remembers it', async () => {
    const user = userEvent.setup();
    await renderToolbar(runningPanel());

    await user.click(screen.getByRole('checkbox', { name: 'Labels' }));

    expect(screen.getByRole('button', { name: 'Check against Alpaca' }).textContent?.trim()).toBe('Check against Alpaca');
    expect(localStorage.getItem('bot-page.toolbar.labels.v1')).toBe('on');
  });

  it('offers a finished bot’s Deploy again as its filled, named primary link', async () => {
    const panel = fakeBotPanelView({ health: { ...fakeBotPanelView().health, running: false } });
    await renderToolbar(panel);

    const again = screen.getByRole('link', { name: 'Deploy again' });
    expect(again.textContent?.trim()).toBe('Deploy again');
    expect(again.className).toContain('toolbar__button--primary');
  });
});
