import { render, screen } from '@testing-library/angular';
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

    screen.getByRole('button', { name: 'Stop' }).click();

    expect(triggered).toHaveBeenCalledWith(presented);
  });

  it('does not emit while the action is disabled', async () => {
    const triggered = vi.fn();
    await render(PanelActionButtonComponent, {
      inputs: { action: action({ enabled: false }) },
      on: { triggered },
    });

    screen.getByRole('button', { name: 'Stop' }).click();

    expect(triggered).not.toHaveBeenCalled();
  });

  it('renders backend-presented confirmation and blockers', async () => {
    await render(PanelActionButtonComponent, {
      inputs: {
        action: action({
          blockers: [
            {
              code: 'OPEN_ORDER',
              label: 'An order is still open.',
              explanation: 'Wait for the order to settle.',
              disposition: 'wait',
              action_hint: null,
            },
          ],
          confirmation: {
            required: true,
            prompt: 'This stops the bot.',
            ack_phrase: null,
          },
        }),
      },
    });

    expect(screen.getByText('This stops the bot.')).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toContain(
      'An order is still open.',
    );
  });
});
