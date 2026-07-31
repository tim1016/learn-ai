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
  it('emits the presented action when enabled', async () => {
    const triggered = vi.fn();
    const presented = action();
    await render(PanelActionButtonComponent, {
      inputs: { action: presented },
      on: { triggered },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));

    expect(triggered).toHaveBeenCalledWith(presented);
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
});
