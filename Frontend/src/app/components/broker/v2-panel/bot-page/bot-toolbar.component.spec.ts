/** #2794 R2, R6: the bot page's toolbar renders the backend's action list --
 * offered actions as icon buttons, blocked ones disabled with their reason,
 * those past the bar's room named under More, not-needed ones only in All
 * actions. */
import { provideRouter } from '@angular/router';
import { render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { fakeBotPage, fakeBotPanelView, fakePanelAction, fakeSqliteStopAction } from '../../../../testing/bot-panel-fixtures';
import type { ManualOrderTicketNavigation } from '../../lib/manual-order-navigation';
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

async function renderToolbar(panel: BotPanelView, manualOrder: ManualOrderTicketNavigation | null = null) {
  const actionRequested = vi.fn();
  const buildProof = vi.fn();
  const view = await render(BotToolbarComponent, {
    inputs: { panel, deployAgain: DEPLOY_AGAIN, manualOrder },
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
    // Inspect's actions wait under More.
    expect(within(toolbar).getAllByRole('group').map((group) => group.getAttribute('aria-label')))
      .toEqual(['Bot', 'Fix']);
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

  it('runs an offered custody action through the presented action, and opens Build proof from More', async () => {
    const user = userEvent.setup();
    const { actionRequested, buildProof } = await renderToolbar(runningPanel());

    await user.click(screen.getByRole('button', { name: 'Check against Alpaca' }));
    await user.click(screen.getByRole('button', { name: /^More actions/ }));
    await user.click(within(screen.getByRole('group', { name: 'More actions for this bot' })).getByRole('button', { name: 'Build proof' }));

    expect(actionRequested).toHaveBeenCalledWith(expect.objectContaining({
      action: expect.objectContaining({ action_id: 'reconcile_now' }),
    }));
    expect(buildProof).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('group', { name: 'More actions for this bot' })).toBeNull();
  });

  it('lists every action in All actions -- available, blocked or not needed -- with its reason and system name', async () => {
    const user = userEvent.setup();
    await renderToolbar(runningPanel());

    await user.click(screen.getByRole('button', { name: /^More actions/ }));
    await user.click(screen.getByRole('button', { name: 'All actions' }));

    const list = screen.getByRole('region', { name: 'All actions for this bot' });
    const sell = within(list).getAllByRole('listitem').find((item) => item.textContent?.includes('prepare_safe_flatten'));
    for (const words of ['Sell', 'prepare_safe_flatten', 'This bot holds no shares.', 'Not needed']) {
      expect(sell?.textContent).toContain(words);
    }
    expect(within(list).getAllByRole('listitem')).toHaveLength(6);

    // More and All actions are never open together; Escape hands the keyboard back to More.
    const more = screen.getByRole('button', { name: /^More actions/ });
    await user.click(more);
    expect(screen.queryByRole('region', { name: 'All actions for this bot' })).toBeNull();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('group', { name: 'More actions for this bot' })).toBeNull();
    expect(document.activeElement).toBe(more);
  });

  it('names every icon once Labels is on, and remembers it', async () => {
    const user = userEvent.setup();
    await renderToolbar(runningPanel());

    await user.click(screen.getByRole('button', { name: /^More actions/ }));
    await user.click(screen.getByRole('checkbox', { name: 'Labels' }));

    expect(screen.getByRole('button', { name: 'Check against Alpaca' }).textContent?.trim()).toBe('Check against Alpaca');
    expect(localStorage.getItem('bot-page.toolbar.labels.v1')).toBe('on');
  });

  it('keeps every Bot and Fix action on the bar, whatever else is offered, and names Inspect’s under More', async () => {
    const user = userEvent.setup();
    const panel = runningPanel();
    await renderToolbar({
      ...panel,
      bot_page: {
        ...fakeBotPage(panel),
        toolbar: [
          toolbarEntry({ action_id: 'stop_bot_decisions', label: 'Stop', tone: 'danger', primary: true }),
          toolbarEntry({ action_id: 'prepare_safe_flatten', label: 'Sell', tone: 'danger' }),
          toolbarEntry({ action_id: 'change_end', label: 'Change end' }),
          toolbarEntry({ action_id: 'manual_order', label: 'Manual order' }),
          toolbarEntry({ action_id: 'reconcile_now', label: 'Check against Alpaca', group: 'fix' }),
          toolbarEntry({ action_id: 'cancel_verified_working_orders', label: 'Cancel open orders', group: 'fix' }),
          toolbarEntry({ action_id: 'discharge_attributed_residue', label: 'Write off missing shares', group: 'fix' }),
          toolbarEntry({ action_id: 'open_custody_timeline', label: 'Custody timeline', group: 'inspect' }),
          toolbarEntry({ action_id: 'build_proof', label: 'Build proof', group: 'inspect' }),
        ],
      },
    });

    const toolbar = screen.getByRole('toolbar', { name: 'Actions for this bot' });
    expect(within(toolbar).getByRole('button', { name: 'Sell' }).textContent?.trim()).toBe('Sell');
    expect(within(toolbar).getByRole('button', { name: 'Write off missing shares' })).toBeTruthy();
    expect(within(toolbar).queryByRole('button', { name: 'Custody timeline' })).toBeNull();
    const more = within(toolbar).getByRole('button', { name: 'More actions, 2 not on the bar' });
    expect(more.textContent?.trim()).toBe('2');

    await user.click(more);

    const menu = screen.getByRole('group', { name: 'More actions for this bot' });
    expect(within(menu).getByRole('button', { name: 'Custody timeline' }).textContent?.trim()).toBe('Custody timeline');
    expect(within(menu).getByRole('button', { name: 'Build proof' }).textContent?.trim()).toBe('Build proof');
  });

  it('keeps a blocked Manual order where it is, saying why, and an entry it does not know inert', async () => {
    const user = userEvent.setup();
    const panel = runningPanel();
    const { actionRequested } = await renderToolbar({
      ...panel,
      bot_page: {
        ...fakeBotPage(panel),
        toolbar: [
          toolbarEntry({
            action_id: 'manual_order', label: 'Manual order', availability: 'blocked',
            reason: 'A Dry Run trades no account money.',
          }),
          toolbarEntry({ action_id: 'archive', label: 'Clear' }),
        ],
      },
    }, { commands: ['/brokers', 'alpaca', 'clerks', 'c', 'accounts', 'a'], queryParams: {} });

    expect(screen.queryByRole('link', { name: 'Manual order' })).toBeNull();
    const manual = screen.getByRole('button', { name: 'Manual order' });
    expect(manual.getAttribute('aria-disabled')).toBe('true');
    expect(manual.getAttribute('title')).toBe('A Dry Run trades no account money.');
    await user.click(screen.getByRole('button', { name: 'Clear' }));
    expect(actionRequested).not.toHaveBeenCalled();
  });

  it('keeps the panel’s own controls when a data plane sends no bot_page yet (a rolling deploy)', async () => {
    const panel = { ...runningPanel(), bot_page: null, primary_action: 'stop_bot_decisions' as const };
    await renderToolbar(panel);

    const stop = screen.getByRole('button', { name: fakeSqliteStopAction().label });
    expect(stop.className).toContain('panel-action__button--filled');
    expect(screen.getByRole('button', { name: 'Reconcile now' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Cancel verified working orders' }).getAttribute('aria-disabled')).toBe('true');
  });

  it('offers a finished bot’s Deploy again as its filled, named primary link', async () => {
    const panel = fakeBotPanelView({ health: { ...fakeBotPanelView().health, running: false } });
    await renderToolbar(panel);

    const again = screen.getByRole('link', { name: 'Deploy again' });
    expect(again.textContent?.trim()).toBe('Deploy again');
    expect(again.className).toContain('toolbar__button--primary');
  });
});
