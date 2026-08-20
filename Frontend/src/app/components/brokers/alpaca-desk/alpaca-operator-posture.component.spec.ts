import { fireEvent, render, screen } from '@testing-library/angular';
import { provideRouter, Router } from '@angular/router';
import { describe, expect, it, vi } from 'vitest';

import type { AccountOperatorPosture } from '../../../api/operator-blocker.types';
import { operatorBlockerFixture } from '../../../testing/operator-blocker-fixtures';
import { AlpacaOperatorPostureComponent } from './alpaca-operator-posture.component';

function healthyPosture(): AccountOperatorPosture {
  return {
    condition: null,
    account_desk: null,
    fleet_roster: null,
    status_headline: 'Account Clerk custody is healthy',
    status_detail: 'Durable Clerk state has no active hold or unresolved uncertainty in this scope.',
  };
}

function blockedPosture(
  overrides: Parameters<typeof operatorBlockerFixture>[0] = {},
): AccountOperatorPosture {
  const blocker = operatorBlockerFixture({ host: 'account_desk', ...overrides });
  return {
    condition: blocker.condition,
    account_desk: blocker,
    fleet_roster: { ...blocker, host: 'fleet_roster' },
    status_headline: blocker.headline,
    status_detail: blocker.detail ?? null,
  };
}

describe('AlpacaOperatorPostureComponent', () => {
  it('renders the backend-authored healthy status copy when the condition is null', async () => {
    await render(AlpacaOperatorPostureComponent, {
      inputs: { posture: healthyPosture() },
    });

    expect(screen.getByRole('heading', { name: 'Account Clerk custody is healthy' })).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('fails closed to an explicit unavailable state when posture has not loaded', async () => {
    await render(AlpacaOperatorPostureComponent, {
      inputs: { posture: null },
    });

    expect(screen.getByRole('heading', { name: 'Account status unavailable' })).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('renders the account_desk projection verbatim, not the fleet_roster projection', async () => {
    const blocker = operatorBlockerFixture({
      host: 'account_desk',
      disposition: 'fix_elsewhere',
      headline: 'Desk headline',
    });
    const posture: AccountOperatorPosture = {
      condition: blocker.condition,
      account_desk: blocker,
      fleet_roster: { ...blocker, headline: 'Roster headline', host: 'fleet_roster' },
      status_headline: blocker.headline,
      status_detail: blocker.detail ?? null,
    };

    await render(AlpacaOperatorPostureComponent, { inputs: { posture } });

    expect(screen.getByRole('heading', { name: 'Desk headline' })).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Roster headline' })).toBeNull();
  });

  it('emits the exact OperatorMove for a fix_here disposition the host declares supported', async () => {
    const blockerMoveRequested = vi.fn();
    const move = {
      label: 'Open Clerk recovery',
      action: { kind: 'confirm_in_form' as const, anchor: 'account-desk-clerk-recovery' },
      target: null,
    };
    await render(AlpacaOperatorPostureComponent, {
      inputs: {
        posture: blockedPosture({ disposition: 'fix_here', primaryMove: move }),
        moveIsSupported: () => true,
      },
      on: { blockerMoveRequested },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Open Clerk recovery' }));

    expect(blockerMoveRequested).toHaveBeenCalledWith(move);
  });

  it('suppresses a move the host has not declared support for, instead of a dead click', async () => {
    const blockerMoveRequested = vi.fn();
    await render(AlpacaOperatorPostureComponent, {
      inputs: {
        // Default moveIsSupported only self-dispatches navigate; a
        // confirm_in_form move is not rendered without host opt-in.
        posture: blockedPosture({
          disposition: 'fix_here',
          primaryMove: {
            label: 'Open Clerk recovery',
            action: { kind: 'confirm_in_form' as const, anchor: 'account-desk-clerk-recovery' },
            target: null,
          },
        }),
      },
      on: { blockerMoveRequested },
    });

    expect(screen.queryByRole('button', { name: 'Open Clerk recovery' })).toBeNull();
    expect(screen.getByText('This requires attention outside this desk.')).toBeTruthy();
    expect(blockerMoveRequested).not.toHaveBeenCalled();
  });

  it('navigates for a fix_elsewhere disposition instead of emitting a move', async () => {
    const view = await render(AlpacaOperatorPostureComponent, {
      inputs: {
        posture: blockedPosture({
          disposition: 'fix_elsewhere',
          headline: 'Broker connection needs attention',
        }),
      },
      providers: [provideRouter([])],
    });

    const router = view.fixture.debugElement.injector.get(Router);
    const navigate = vi.spyOn(router, 'navigate').mockResolvedValue(true);
    fireEvent.click(screen.getByRole('button', { name: 'Connect the broker' }));

    expect(navigate).toHaveBeenCalledWith(['/broker'], { fragment: undefined });
  });

  it('uses the wait disposition without inventing a repair', async () => {
    await render(AlpacaOperatorPostureComponent, {
      inputs: { posture: blockedPosture({ disposition: 'wait', primaryMove: null }) },
    });

    expect(screen.getByText('Waiting for fresh account evidence.')).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('renders every terminal move as a button when the host declares it supported', async () => {
    const blockerMoveRequested = vi.fn();
    const runbookMove = {
      label: 'Open Clerk recovery runbook',
      action: { kind: 'open_runbook' as const, slug: 'alpaca-account-clerk-authority-recovery' },
      target: null,
    };
    await render(AlpacaOperatorPostureComponent, {
      inputs: {
        posture: blockedPosture({
          disposition: 'terminal',
          primaryMove: runbookMove,
          headline: 'Manual escalation required',
        }),
        moveIsSupported: () => true,
      },
      on: { blockerMoveRequested },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Open Clerk recovery runbook' }));

    expect(blockerMoveRequested).toHaveBeenCalledWith(runbookMove);
  });

  it('falls back to the outside-desk message for an unsupported terminal runbook move', async () => {
    await render(AlpacaOperatorPostureComponent, {
      inputs: {
        // Default moveIsSupported does not include open_runbook.
        posture: blockedPosture({
          disposition: 'terminal',
          primaryMove: {
            label: 'Open Clerk recovery runbook',
            action: { kind: 'open_runbook' as const, slug: 'alpaca-account-clerk-authority-recovery' },
            target: null,
          },
          headline: 'Manual escalation required',
        }),
      },
    });

    expect(screen.queryByRole('button', { name: 'Open Clerk recovery runbook' })).toBeNull();
    expect(screen.getByText('This requires attention outside this desk.')).toBeTruthy();
  });
});
