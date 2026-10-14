import { render, screen, within } from '@testing-library/angular';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { SqliteSafeFlattenPlan } from '../../../../api/alpaca.types';
import { formatTimestampDisplay } from '../../../../shared/timestamp';
import { SafeFlattenPlanComponent } from './safe-flatten-plan.component';

const PLAN: SqliteSafeFlattenPlan = {
  version_token: 'plan-token-17-exact',
  account_id: 'PA1',
  authority_generation: 4,
  db_identity_token: 'db-generation-4',
  control_revision: 17,
  scope: 'CUSTODY_SUBJECT',
  strategy_instance_id: 'spy-bot',
  reconciliation_id: 'reconciliation-17',
  account_check_kind: 'reconcile_now',
  account_check_observed_at_ms: 1_700_000_009_000,
  prepared_at_ms: 1_700_000_010_000,
  expires_at_ms: 1_700_000_039_000,
  legs: [{
    strategy_instance_id: 'spy-bot',
    symbol: 'SPY',
    side: 'sell',
    quantity: 1.25,
    position_updated_at_ms: 1_700_000_008_000,
  }],
};

describe('SafeFlattenPlanComponent', () => {
  afterEach(() => vi.useRealTimers());

  it('renders the exact backend-authored plan without implying execution', async () => {
    await render(SafeFlattenPlanComponent, {
      inputs: { plan: { ...PLAN, expires_at_ms: Date.now() + 30_000 } },
    });

    expect(screen.getByRole('region', {
      name: 'Prepared safe-flatten reduction plan',
    })).toBeTruthy();
    expect(screen.getByText('Spy')).toBeTruthy();
    expect(screen.getByText('Sell')).toBeTruthy();
    expect(screen.getByText('1.25')).toBeTruthy();
    expect(screen.getByText('spy-bot')).toBeTruthy();
    expect(screen.getByText('db-generation-4')).toBeTruthy();
    expect(screen.getByText('reconciliation-17')).toBeTruthy();
    expect(screen.getByText('plan-token-17-exact')).toBeTruthy();
    expect(screen.getByText(/No order has been submitted/)).toBeTruthy();
    expect(screen.queryByText('Expired')).toBeNull();
  });

  /** The definition of the "Account check" term: what the plan says it rests on. */
  function accountCheck(): HTMLElement {
    const definition = screen.getByText('Account check').nextElementSibling;
    if (!(definition instanceof HTMLElement)) throw new Error('The plan shows no account check.');
    return definition;
  }

  it('names the Reconcile now receipt a plan rests on, and when it was made', async () => {
    await render(SafeFlattenPlanComponent, {
      inputs: { plan: { ...PLAN, expires_at_ms: Date.now() + 30_000 } },
    });

    const check = within(accountCheck());
    expect(check.getByText('Reconcile now')).toBeTruthy();
    expect(check.getByText('reconciliation-17')).toBeTruthy();
    expect(check.getByText(formatTimestampDisplay(PLAN.account_check_observed_at_ms, { mode: 'local' }))).toBeTruthy();
  });

  it("says a plan rests on the Account Clerk's own check, never on a Reconcile now that was not run", async () => {
    // #2839: the Clerk's periodic check writes no receipt, so the plan carries no reconciliation id.
    const checkedAtMs = PLAN.prepared_at_ms - 12_000;
    await render(SafeFlattenPlanComponent, {
      inputs: {
        plan: {
          ...PLAN,
          reconciliation_id: null,
          account_check_kind: 'clerk_pass',
          account_check_observed_at_ms: checkedAtMs,
          expires_at_ms: Date.now() + 48_000,
        },
      },
    });

    const check = within(accountCheck());
    expect(check.getByText("The Account Clerk's own check")).toBeTruthy();
    expect(check.getByText(formatTimestampDisplay(checkedAtMs, { mode: 'local' }))).toBeTruthy();
    expect(screen.queryByText('Reconcile now')).toBeNull();
    expect(screen.queryByText('reconciliation-17')).toBeNull();
    // The rest of the plan reads as any other: nothing was sent, and it is not expired.
    expect(screen.getByText(/No order has been submitted/)).toBeTruthy();
    expect(screen.queryByText('Expired')).toBeNull();
  });

  it('marks the plan expired when its deadline passes', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(PLAN.prepared_at_ms);
    const { fixture } = await render(SafeFlattenPlanComponent, {
      inputs: { plan: { ...PLAN, expires_at_ms: PLAN.prepared_at_ms + 1_000 } },
    });

    vi.setSystemTime(PLAN.prepared_at_ms + 1_000);
    await vi.advanceTimersByTimeAsync(1_000);
    fixture.detectChanges();

    expect(screen.getByText('Expired')).toBeTruthy();
    expect(screen.getByText(/Prepare safe flatten again/)).toBeTruthy();
    expect(screen.getByText(/No order has been submitted/)).toBeTruthy();
  });

  it.each([
    ['an unexpired', { ...PLAN, expires_at_ms: Date.now() + 30_000 }],
    ['an expired', PLAN],
  ])('does not say no order was submitted on %s plan whose send got no answer (#3178)', async (_name, plan) => {
    await render(SafeFlattenPlanComponent, { inputs: { plan, sendOutcomeUnknown: true } });

    expect(screen.queryByText(/No order has been submitted/)).toBeNull();
    expect(screen.getByText(/whether an order was submitted is not known/)).toBeTruthy();
    expect(screen.getByText(/Check the position with Alpaca before another order/)).toBeTruthy();
  });
});
