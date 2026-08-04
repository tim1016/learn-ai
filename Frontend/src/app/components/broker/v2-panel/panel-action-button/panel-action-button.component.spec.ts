import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { PanelAction } from '../lib/broker-v2-panel.types';
import { PanelActionButtonComponent } from './panel-action-button.component';

function action(overrides: Partial<PanelAction> = {}): PanelAction {
  return {
    action_id: 'stop',
    label: 'Stop',
    explanation: 'Stop after the current bar.',
    enabled: true,
    blockers: [],
    confirmation: null,
    revision: 2,
    concurrency_token: 'token',
    ...overrides,
  };
}

describe('PanelActionButtonComponent', () => {
  it('does not mount a closed confirmation dialog', async () => {
    const view = await render(PanelActionButtonComponent, {
      inputs: {
        action: action({
          confirmation: {
            title: 'Stop bot',
            body: 'This stops the bot.',
            consequence: 'The bot will stop evaluating bars.',
            confirm_label: 'Stop',
          },
        }),
      },
    });

    expect(view.fixture.nativeElement.querySelector('dialog')).toBeNull();
  });

  it('emits the presented action when enabled', async () => {
    const triggered = vi.fn();
    const presented = action();
    await render(PanelActionButtonComponent, {
      inputs: { action: presented },
      on: { triggered },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));

    expect(triggered).toHaveBeenCalledWith({ action: presented, reason: null });
  });

  it('does not emit while the action is disabled', async () => {
    const triggered = vi.fn();
    await render(PanelActionButtonComponent, {
      inputs: { action: action({ enabled: false }) },
      on: { triggered },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));

    expect(triggered).not.toHaveBeenCalled();
  });

  it('renders backend-presented confirmation and blockers', async () => {
    await render(PanelActionButtonComponent, {
      inputs: {
        action: action({
          blockers: [
            {
              condition: {
                id: 'OPEN_ORDER',
                severity: 'blocking',
                scope: 'bot',
              },
              host: 'bot_cockpit',
              anchor: { kind: 'surface', subject_key: null },
              audience: 'both',
              disposition: 'wait',
              headline: 'An order is still open.',
              detail: 'Wait for the order to settle.',
              primary_move: null,
              secondary_moves: [],
              applies_to: 'run',
            },
          ],
          confirmation: {
            title: 'Stop bot',
            body: 'This stops the bot.',
            consequence: 'The bot will stop evaluating bars.',
            confirm_label: 'Stop',
          },
        }),
      },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    expect(screen.getByText('This stops the bot.')).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toContain(
      'An order is still open.',
    );
  });

  it('can suppress only the blocker its parent already presents', async () => {
    await render(PanelActionButtonComponent, {
      inputs: {
        action: action({
          enabled: false,
          blockers: [
            {
              condition: {
                id: 'BOT_ALREADY_STOPPED',
                severity: 'blocking',
                scope: 'bot',
              },
              host: 'bot_cockpit',
              anchor: { kind: 'surface', subject_key: null },
              audience: 'both',
              disposition: 'terminal',
              headline: 'The bot is already stopped.',
              detail: 'No stop command is necessary.',
              primary_move: null,
              secondary_moves: [],
              applies_to: 'run',
            },
            {
              condition: {
                id: 'ACCOUNT_CUSTODY_UNPROVABLE',
                severity: 'blocking',
                scope: 'account',
              },
              host: 'bot_cockpit',
              anchor: { kind: 'surface', subject_key: null },
              audience: 'both',
              disposition: 'fix_elsewhere',
              headline: 'The Clerk cannot prove current account custody.',
              detail: 'Restore broker observation and reconcile.',
              primary_move: null,
              secondary_moves: [],
              applies_to: 'run',
            },
          ],
        }),
        suppressedBlockerId: 'BOT_ALREADY_STOPPED',
      },
    });

    expect(screen.queryByText('The bot is already stopped.')).toBeNull();
    expect(screen.getByRole('alert').textContent).toContain(
      'The Clerk cannot prove current account custody.',
    );
    expect(screen.getByRole('button', { name: 'Stop' })).toBeTruthy();
  });

  it('does not emit a destructive action until the backend token is confirmed', async () => {
    const triggered = vi.fn();
    await render(PanelActionButtonComponent, {
      inputs: {
        action: action({
          action_id: 'flatten_stop',
          label: 'Flatten & stop',
          confirmation: {
            title: 'Flatten attributed exposure?',
            body: 'SPY 2; one working order.',
            consequence: 'The runtime stops before reducing orders are submitted.',
            confirm_label: 'Flatten & stop',
            required_token: 'FLATTEN',
          },
        }),
      },
      on: { triggered },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Flatten & stop' }));
    expect(triggered).not.toHaveBeenCalled();
    const submit = screen.getByTestId('typed-halt-confirm-submit') as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    fireEvent.input(screen.getByTestId('typed-halt-confirm-input'), {
      target: { value: 'FLATTEN' },
    });
    fireEvent.click(submit);
    expect(triggered).toHaveBeenCalledTimes(1);
  });

  it('renders the comment-capable confirm (not the typed-halt confirm) for a comment-required action', async () => {
    await render(PanelActionButtonComponent, {
      inputs: {
        action: action({
          action_id: 'record_inventory_baseline',
          label: 'Recover inventory baseline',
          confirmation: {
            title: 'Adopt current broker inventory?',
            body: 'Reads the current Alpaca positions.',
            consequence: 'Earlier trades remain in audit history.',
            confirm_label: 'Recover inventory baseline',
            required_token: 'BASELINE',
          },
        }),
      },
    });

    fireEvent.click(
      screen.getByRole('button', { name: 'Recover inventory baseline' }),
    );

    expect(
      screen.getByTestId('panel-action-comment-confirm-reason'),
    ).toBeTruthy();
    expect(screen.queryByTestId('typed-halt-confirm-input')).toBeNull();
  });

  it('requires a non-blank comment AND the typed token before emitting a comment-required action', async () => {
    const triggered = vi.fn();
    const recordAction = action({
      action_id: 'record_inventory_baseline',
      label: 'Recover inventory baseline',
      confirmation: {
        title: 'Adopt current broker inventory?',
        body: 'Reads the current Alpaca positions.',
        consequence: 'Earlier trades remain in audit history.',
        confirm_label: 'Recover inventory baseline',
        required_token: 'BASELINE',
      },
    });
    await render(PanelActionButtonComponent, {
      inputs: { action: recordAction },
      on: { triggered },
    });

    fireEvent.click(
      screen.getByRole('button', { name: 'Recover inventory baseline' }),
    );
    const submit = screen.getByTestId(
      'panel-action-comment-confirm-submit',
    ) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);

    fireEvent.input(screen.getByTestId('panel-action-comment-confirm-reason'), {
      target: { value: 'Recovering after a killed process.' },
    });
    expect(submit.disabled).toBe(true); // token still empty

    fireEvent.input(screen.getByTestId('panel-action-comment-confirm-token'), {
      target: { value: 'BASELINE' },
    });
    expect(submit.disabled).toBe(false);

    fireEvent.click(submit);
    expect(triggered).toHaveBeenCalledWith({
      action: recordAction,
      reason: 'Recovering after a killed process.',
    });
  });

  it('emits the action with a null reason for a clear_hold-style comment-required action with no required_token', async () => {
    const triggered = vi.fn();
    const clearHoldAction = action({
      action_id: 'clear_hold',
      label: 'Clear hold',
      confirmation: {
        title: 'Clear the account hold?',
        body: 'This clears the hold on the account.',
        consequence: 'Order submission resumes immediately.',
        confirm_label: 'Clear hold',
      },
    });
    await render(PanelActionButtonComponent, {
      inputs: { action: clearHoldAction },
      on: { triggered },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Clear hold' }));
    fireEvent.input(screen.getByTestId('panel-action-comment-confirm-reason'), {
      target: { value: 'Broker confirms flat; stale hold.' },
    });
    fireEvent.click(screen.getByTestId('panel-action-comment-confirm-submit'));

    expect(triggered).toHaveBeenCalledWith({
      action: clearHoldAction,
      reason: 'Broker confirms flat; stale hold.',
    });
  });
});
