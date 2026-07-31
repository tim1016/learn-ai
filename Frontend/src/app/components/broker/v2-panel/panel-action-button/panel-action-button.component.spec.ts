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

    expect(screen.getByText('This stops the bot.')).toBeTruthy();
    expect(screen.getByRole('alert').textContent).toContain(
      'An order is still open.',
    );
  });
});
