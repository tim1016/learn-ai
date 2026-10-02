import { render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { RecentDecisionsListComponent } from './recent-decisions-list.component';
import type { RecentDecisionView, StrategyViewResponse } from '../../lib/broker-v2-panel.types';
import { formatTimestampDisplay } from '../../../../../shared/timestamp/timestamp-display';
import {
  BEFORE_START_TEXT,
  STRATEGY_RUN_STARTED_AT_MS,
  barCloseMs,
  fakeRecentDecision,
  fakeStrategyView,
} from '../../../../../testing/strategy-view-fixtures';

const SYNTHETIC_DECISION: RecentDecisionView = {
  seq: 5,
  recorded_at_ms: 1_753_800_000_000,
  outcome: 'entered',
  reason_code: 'CROSS_UP',
  bar_ref: 'SPY@1753799999900',
  order_ref: 'simulated:run-dry:1753799999900:ENTER',
  simulated: true,
  authority_account_id: 'sim:bot-alpha',
  authority_kind: 'synthetic',
};

/** The panel lists decisions newest first. */
const RUN_DECISIONS = [fakeRecentDecision(12, 3), fakeRecentDecision(11, 2)];

async function renderList(
  decisions: readonly RecentDecisionView[],
  strategyView: StrategyViewResponse | null = null,
  selectedBarCloseMs: number | null = null,
) {
  const selections = vi.fn();
  const rendered = await render(RecentDecisionsListComponent, {
    inputs: { decisions, strategyView, selectedBarCloseMs },
    on: { selectedBarCloseMs: selections },
  });
  return { ...rendered, selections };
}

function rows(): HTMLElement[] {
  return within(screen.getByRole('list', { name: 'Recent decisions' })).getAllByRole('listitem')
    .filter((item) => item.parentElement?.getAttribute('aria-label') === 'Recent decisions');
}

describe('RecentDecisionsListComponent', () => {
  it('says "No action" once when the reason only restates the outcome', async () => {
    await renderList([{ ...SYNTHETIC_DECISION, outcome: 'no_action', reason_code: 'NO_ACTION' }]);

    expect(screen.getAllByText('No Action')).toHaveLength(1);
  });

  it('renders each decision with its authority visibly distinguishable (issue #1729 AC #8)', async () => {
    await renderList([SYNTHETIC_DECISION]);

    expect(screen.getByRole('list', { name: 'Recent decisions' })).toBeTruthy();
    expect(screen.getByText('Entered')).toBeTruthy();
    expect(screen.getByText('Cross Up')).toBeTruthy();
    expect(screen.getByText('Synthetic')).toBeTruthy();
    // #2183: "Recent decisions" carries the eyebrow look itself now; the
    // separate "Simulation" label above it is retired.
    expect(screen.getByRole('heading', { name: 'Recent decisions' })).toBeTruthy();
    expect(screen.queryByText('Simulation')).toBeNull();
  });

  it('says a decision recorded before decisions saved their values has none', async () => {
    await renderList([SYNTHETIC_DECISION]);

    expect(screen.getByText('values not recorded')).toBeTruthy();
    expect(screen.queryByRole('list', { name: 'Checks' })).toBeNull();
  });

  it('shows each check as a ✓/✗ chip, the rules that applied first, and expands to the checks table', async () => {
    const user = userEvent.setup();
    await renderList([fakeRecentDecision(11, 2)]);

    const chips = within(screen.getByRole('list', { name: 'Checks' })).getAllByRole('listitem');
    expect(chips.map((chip) => chip.textContent?.replace(/\s+/g, ' ').trim())).toEqual([
      '✗ zap failed',
      '✓ Bar 52.0 passed',
      '✗ quit in 3 failed',
    ]);
    expect(chips[2].classList).toContain('chip--idle');
    expect(screen.queryByRole('table', { name: 'Checks' })).toBeNull();

    await user.click(screen.getByRole('button', { name: /No Action/ }));

    const table = screen.getByRole('table', { name: 'Checks' });
    expect(within(table).getByRole('rowheader', { name: 'Zap' })).toBeTruthy();
    expect(within(table).getByText('no zap at bar 2')).toBeTruthy();
    expect(within(table).getByText(/needs a zap up/)).toBeTruthy();
    expect(screen.getByText('Foo 7 102.00 · Bar 3 52.0 · Baz 1 1.0 · bot’s own values')).toBeTruthy();
  });

  it('selects the decision’s candle when a row is chosen, and highlights the row of a selected candle', async () => {
    const user = userEvent.setup();
    const { selections } = await renderList(RUN_DECISIONS, null, barCloseMs(3));

    const [older, newer] = rows();
    expect(newer.getAttribute('aria-current')).toBe('true');
    expect(older.getAttribute('aria-current')).toBeNull();

    await user.click(within(older).getByRole('button'));

    expect(selections).toHaveBeenLastCalledWith(barCloseMs(2));
  });

  it('lists the newest bar from before the start above a "Bot started" divider, oldest first', async () => {
    await renderList(RUN_DECISIONS, fakeStrategyView());

    const items = rows().map((item) => item.textContent?.replace(/\s+/g, ' ').trim() ?? '');
    const minute = (ms: number) => formatTimestampDisplay(ms, { mode: 'local', granularity: 'minute' });
    expect(items[0]).toContain(`${minute(barCloseMs(1))} ${BEFORE_START_TEXT}`);
    expect(items[1]).toBe(`Bot started ${minute(STRATEGY_RUN_STARTED_AT_MS)}`);
    expect(items[2]).toContain(`${minute(barCloseMs(2))} No Action`);
    expect(items[3]).toContain(`${minute(barCloseMs(3))} No Action`);
  });

  it('draws no "Bot started" divider once the run’s first decision has left the list', async () => {
    await renderList([fakeRecentDecision(12, 3)], fakeStrategyView());

    expect(screen.queryByText(/Bot started/)).toBeNull();
    expect(screen.queryByText(BEFORE_START_TEXT)).toBeNull();
  });

  it('draws no "Bot started" divider when the run has decisions the view could not include', async () => {
    // The run's first decision predates saved values, so the first decision the
    // view draws is not where the run began.
    await renderList(RUN_DECISIONS, fakeStrategyView({ unexplained_decision_count: 1 }));

    expect(screen.queryByText(/Bot started/)).toBeNull();
  });
});
