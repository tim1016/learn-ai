import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { fakeSqliteStopAction } from '../../../../testing/bot-panel-fixtures';
import type { PanelAction } from '../lib/broker-v2-panel.types';
import { PanelActionButtonComponent } from './panel-action-button.component';

/** A neutral unconfirmed action, as the SQLite panel presents Reconcile now. */
function action(overrides: Partial<PanelAction> = {}): PanelAction {
  return {
    action_id: 'reconcile_now',
    label: 'Reconcile now',
    explanation: 'Compare durable Clerk custody with a fresh Alpaca account observation.',
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
      inputs: { action: fakeSqliteStopAction() },
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

    fireEvent.click(screen.getByRole('button', { name: 'Reconcile now' }));

    expect(triggered).toHaveBeenCalledWith({ action: presented, reason: null });
  });

  it('does not emit while the action is disabled', async () => {
    const triggered = vi.fn();
    await render(PanelActionButtonComponent, {
      inputs: { action: action({ enabled: false }) },
      on: { triggered },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Reconcile now' }));

    expect(triggered).not.toHaveBeenCalled();
  });

  it('keeps a blocked action keyboard-focusable so its reason is reachable', async () => {
    // Regression: the button must stay off the native `disabled` attribute
    // (which removes it from the tab order) and rely on `aria-disabled` plus
    // trigger()'s own guard instead — otherwise a keyboard/screen-reader
    // user can never reach the blocker explanation this button describes.
    const triggered = vi.fn();
    await render(PanelActionButtonComponent, {
      inputs: {
        action: action({
          enabled: false,
          blockers: [
            {
              condition: { id: 'OPEN_ORDER', severity: 'blocking', scope: 'bot' },
              host: 'bot_cockpit',
              anchor: { kind: 'surface', subject_key: null },
              disposition: 'wait',
              headline: 'An order is still open.',
              detail: 'Wait for the order to settle.',
              primary_move: null,
              secondary_moves: [],
              applies_to: 'run',
            },
          ],
        }),
      },
      on: { triggered },
    });

    const button = screen.getByRole('button', { name: 'Reconcile now' }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    expect(button.getAttribute('aria-disabled')).toBe('true');
    expect(button.getAttribute('aria-describedby')).toBeTruthy();
    button.focus();
    expect(document.activeElement).toBe(button);

    fireEvent.click(button);
    expect(triggered).not.toHaveBeenCalled();
  });

  it('renders backend-presented confirmation and blockers', async () => {
    await render(PanelActionButtonComponent, {
      inputs: {
        action: fakeSqliteStopAction({
          blockers: [
            {
              condition: {
                id: 'OPEN_ORDER',
                severity: 'blocking',
                scope: 'bot',
              },
              host: 'bot_cockpit',
              anchor: { kind: 'surface', subject_key: null },
              disposition: 'wait',
              headline: 'An order is still open.',
              detail: 'Wait for the order to settle.',
              primary_move: null,
              secondary_moves: [],
              applies_to: 'run',
            },
          ],
        }),
      },
    });

    const button = screen.getByRole('button', { name: 'Stop bot decisions' }) as HTMLButtonElement;
    expect(button.title).toContain('Open Order');
    expect(button.title).toContain('An order is still open.');
    expect(button.getAttribute('aria-describedby')).toBeTruthy();

    fireEvent.click(button);
    expect(screen.getByText('Stop the bot making new decisions. Stopping doesn\'t sell its shares.')).toBeTruthy();
  });

  it('does not emit a destructive action until the backend token is confirmed', async () => {
    const triggered = vi.fn();
    await render(PanelActionButtonComponent, {
      inputs: {
        action: action({
          action_id: 'execute_safe_flatten',
          label: 'Execute safe flatten',
          confirmation: {
            title: 'Flatten attributed exposure?',
            body: 'SPY 2.',
            consequence: 'The Clerk will submit reduction-only orders for the exact attributed quantities.',
            confirm_label: 'Flatten now',
            required_token: 'FLATTEN',
          },
        }),
      },
      on: { triggered },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Execute safe flatten' }));
    expect(triggered).not.toHaveBeenCalled();
    const submit = screen.getByTestId('typed-halt-confirm-submit') as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    fireEvent.input(screen.getByTestId('typed-halt-confirm-input'), {
      target: { value: 'FLATTEN' },
    });
    fireEvent.click(submit);
    expect(triggered).toHaveBeenCalledTimes(1);
  });

  it('hands an action whose host opens its own ticket straight to the host, with no dialog here (#2854)', async () => {
    const triggered = vi.fn();
    const presented = fakeSqliteStopAction();
    await render(PanelActionButtonComponent, {
      inputs: { action: presented, opensTicket: true },
      on: { triggered },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Stop bot decisions' }));

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(triggered).toHaveBeenCalledExactlyOnceWith({ action: presented, reason: null });
  });

  // ── Story 80: a cancelled confirmation hands the keyboard back ────────────
  // Every real host mounts the dialog under its own `@if` and removes it on
  // cancel, so the return has to survive the dialog being destroyed.

  describe('focus after a cancelled confirmation', () => {
    const confirmed = fakeSqliteStopAction();

    async function openConfirmation(): Promise<HTMLElement> {
      await render(PanelActionButtonComponent, { inputs: { action: confirmed } });
      const opener = screen.getByRole('button', { name: 'Stop bot decisions' });
      opener.focus();
      fireEvent.click(opener);
      await Promise.resolve();
      expect(screen.getByTestId('typed-halt-confirm-dialog')).toBeTruthy();
      expect(document.activeElement).not.toBe(opener);
      return opener;
    }

    it('returns focus to the action button when Cancel is pressed', async () => {
      const opener = await openConfirmation();

      fireEvent.click(screen.getByTestId('typed-halt-confirm-cancel'));
      await new Promise((resolve) => setTimeout(resolve));

      expect(screen.queryByTestId('typed-halt-confirm-dialog')).toBeNull();
      expect(document.activeElement).toBe(opener);
    });

    it('returns focus to the action button when Escape is pressed', async () => {
      const opener = await openConfirmation();

      fireEvent.keyDown(screen.getByTestId('typed-halt-confirm-cancel'), { key: 'Escape' });
      await new Promise((resolve) => setTimeout(resolve));

      expect(screen.queryByTestId('typed-halt-confirm-dialog')).toBeNull();
      expect(document.activeElement).toBe(opener);
    });
  });
});
