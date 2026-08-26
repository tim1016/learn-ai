import { fireEvent, render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import type { OperatorBlocker, OperatorMove } from '../../../../api/operator-blocker.types';
import { BOT_COCKPIT_RECONCILE_ANCHOR } from '../../../../api/operator-blocker.types';
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
    expect(screen.getByRole('alert').textContent).toContain('Open Order');
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
        suppressedBlockerReasonCode: 'BOT_ALREADY_STOPPED',
      },
    });

    expect(screen.queryByText('The bot is already stopped.')).toBeNull();
    expect(screen.getByText('Bot Already Stopped')).toBeTruthy();
    expect(
      screen.getAllByRole('alert').some((alert) =>
        alert.textContent?.includes('The Clerk cannot prove current account custody.'),
      ),
    ).toBe(true);
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

  // ── S17: the cure the backend authored has to be reachable ────────────────
  // `_capability_blocker` authors stale evidence as `fix_here` and attaches a
  // reconcile move. Rendering the blocker prose alone left that cure invisible.

  const reconcileMove: OperatorMove = {
    label: 'Reconcile this account now',
    action: { kind: 'confirm_in_form', anchor: BOT_COCKPIT_RECONCILE_ANCHOR },
    target: null,
  };

  function staleBlocker(overrides: Partial<OperatorBlocker> = {}): OperatorBlocker {
    return {
      condition: {
        id: 'RECOVERY_EVIDENCE_STALE',
        severity: 'blocking',
        scope: 'account',
        evidence: {},
      },
      host: 'bot_cockpit',
      anchor: { kind: 'surface', subject_key: null },
      audience: 'both',
      disposition: 'fix_here',
      headline: 'Clerk evidence for this account is stale.',
      detail: 'Reconcile to refresh it.',
      primary_move: reconcileMove,
      secondary_moves: [],
      applies_to: 'run',
      ...overrides,
    };
  }

  const supportsReconcile = (move: OperatorMove): boolean =>
    move.action.kind === 'confirm_in_form' &&
    move.action.anchor === BOT_COCKPIT_RECONCILE_ANCHOR;

  it('renders a fix_here blocker cure as a dispatchable control', async () => {
    const moveRequested = vi.fn();
    await render(PanelActionButtonComponent, {
      inputs: {
        action: action({ enabled: false, blockers: [staleBlocker()] }),
        moveIsSupported: supportsReconcile,
      },
      on: { moveRequested },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Reconcile this account now' }));

    expect(moveRequested).toHaveBeenCalledWith(reconcileMove);
  });

  it('renders no cure for a wait blocker', async () => {
    // `wait` rendering nothing is the contract, not a gap: the cure is
    // genuinely elsewhere, so offering a move here would be a lie.
    await render(PanelActionButtonComponent, {
      inputs: {
        action: action({
          enabled: false,
          blockers: [staleBlocker({ disposition: 'wait' })],
        }),
        moveIsSupported: supportsReconcile,
      },
    });

    expect(
      screen.queryByRole('button', { name: 'Reconcile this account now' }),
    ).toBeNull();
  });

  it('keeps the cure reachable when the parent suppresses the blocker prose', async () => {
    // Suppression de-duplicates the *prose* a gate already prints; the cure
    // is not prose, and suppressing it is what made the move invisible.
    await render(PanelActionButtonComponent, {
      inputs: {
        action: action({ enabled: false, blockers: [staleBlocker()] }),
        suppressedBlockerId: 'RECOVERY_EVIDENCE_STALE',
        suppressedBlockerReasonCode: 'RECOVERY_EVIDENCE_STALE',
        moveIsSupported: supportsReconcile,
      },
    });

    expect(screen.queryByText('Clerk evidence for this account is stale.')).toBeNull();
    expect(
      screen.getByRole('button', { name: 'Reconcile this account now' }),
    ).toBeTruthy();
  });

  it('renders the cure control accessibly', async () => {
    await render(PanelActionButtonComponent, {
      inputs: {
        action: action({ enabled: false, blockers: [staleBlocker()] }),
        moveIsSupported: supportsReconcile,
      },
    });

    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false } },
    });
    expect(results.violations).toEqual([]);
  });

  it('offers no move the host cannot dispatch', async () => {
    // Default support covers only self-dispatchable moves. An unsupported
    // anchor must render no button rather than a click that does nothing.
    await render(PanelActionButtonComponent, {
      inputs: { action: action({ enabled: false, blockers: [staleBlocker()] }) },
    });

    expect(
      screen.queryByRole('button', { name: 'Reconcile this account now' }),
    ).toBeNull();
  });

});
