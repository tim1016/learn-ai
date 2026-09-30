import { provideRouter } from '@angular/router';
import { render, screen, fireEvent } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { TEST_ACCOUNT_ID, TEST_CLERK_ID, testLane } from '../fleet/fleet-directory-testing';
import type { LaneDescriptor } from '../fleet/fleet-directory.types';
import {
  LaneAttentionService,
  QUIET_LANE_ATTENTION_STATE,
  UNPOLLED_LANE_ATTENTION_STATE,
  type LaneAttentionItem,
  type LaneAttentionState,
} from '../services/lane-attention.service';
import { LaneAttentionBellComponent } from './lane-attention-bell.component';

function item(overrides: Partial<LaneAttentionItem> = {}): LaneAttentionItem {
  return {
    condition_id: 'unc-1',
    reason_code: 'EXIT_NOT_FLAT',
    kind: 'exit',
    action: { label: 'Open bot', destination: 'bot' },
    severity: 'blocking',
    strategy_instance_id: 'ema-1',
    symbol: 'SPY',
    headline: 'This bot’s exit has not flattened its position',
    ...overrides,
  };
}

async function renderBell(
  state: LaneAttentionState,
  lane: LaneDescriptor = testLane(),
) {
  return render(LaneAttentionBellComponent, {
    inputs: { lane },
    providers: [
      provideRouter([]),
      {
        provide: LaneAttentionService,
        useValue: { stateFor: (clerkId: string) => (clerkId === lane.clerk_id ? state : UNPOLLED_LANE_ATTENTION_STATE) },
      },
    ],
  });
}

function bellButton(): HTMLElement {
  return screen.getByRole('button', { name: /attention/i });
}

describe('LaneAttentionBellComponent', () => {
  it('offers the backend Flatten action for a stopped bot still holding', async () => {
    await renderBell({ unknown: false, errorReason: null, items: [item({
      kind: 'stopped_holding', reason_code: 'STOPPED_STILL_HOLDING', severity: 'warning',
      headline: 'ema-1 is stopped but still holds 5 SPY. No bot is managing it.',
      action: { label: 'Flatten…', destination: 'bot' },
    })] });
    await fireEvent.click(bellButton());
    expect(screen.getByText('ema-1 is stopped but still holds 5 SPY. No bot is managing it.')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Flatten…' }).getAttribute('href')).toBe(
      `/brokers/alpaca/clerks/${TEST_CLERK_ID}/accounts/${TEST_ACCOUNT_ID}/bots/ema-1`,
    );
  });

  it('tells the owner a bot’s end sale waits for the open, its code through the receipt label (#2607)', async () => {
    // `lane_summary._end_sale_item`, exactly.
    const headline = 'ema-1 reached its end while the market was closed. Its sale of 5 SPY goes out at the open, Thu Oct 1, 09:30 ET.';
    await renderBell({ unknown: false, errorReason: null, items: [item({
      condition_id: 'end-sale-waits:ema-1', reason_code: 'SCHEDULED_END_WAITS_FOR_OPEN', kind: 'exit',
      severity: 'warning', headline, action: { label: 'Open bot', destination: 'bot' },
    })] });

    await fireEvent.click(bellButton());

    expect(screen.getByText('Warning')).toBeTruthy();
    expect(screen.getByText('Scheduled End Waits For Open')).toBeTruthy();
    expect(screen.getByText(headline)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Open bot' }).getAttribute('href')).toBe(
      `/brokers/alpaca/clerks/${TEST_CLERK_ID}/accounts/${TEST_ACCOUNT_ID}/bots/ema-1`,
    );
  });

  it('links an account-level line to where its fix lives', async () => {
    await renderBell({ unknown: false, errorReason: null, items: [item({
      condition_id: 'hold-1', kind: 'out_of_sync', reason_code: 'UNEXPLAINED_ORDER_HOLD', strategy_instance_id: null,
      headline: 'An order this account did not submit is unreviewed',
      action: { label: 'Open order records', destination: 'activity' },
    })] });
    await fireEvent.click(bellButton());
    expect(screen.getByRole('link', { name: 'Open order records' }).getAttribute('href')).toBe(
      `/brokers/alpaca/clerks/${TEST_CLERK_ID}/accounts/${TEST_ACCOUNT_ID}/activity`,
    );
  });

  it('renders nothing while the lane is quiet', async () => {
    await renderBell(QUIET_LANE_ATTENTION_STATE);
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('renders nothing before the first poll lands', async () => {
    await renderBell(UNPOLLED_LANE_ATTENTION_STATE);
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('shows the count and the severity word for blocking conditions, and lists the item with its way into the bot', async () => {
    await renderBell({
      unknown: false,
      errorReason: null,
      items: [
        item(),
        item({
          condition_id: 'unc-2',
          severity: 'warning',
          headline: 'Order outcome remains unknown',
        }),
      ],
    });

    const bell = bellButton();
    expect(bell.textContent).toContain('2');
    expect(bell.getAttribute('aria-label')).toContain('1 blocking condition');

    await fireEvent.click(bell);
    // One of the two is blocking, so the heading names the blocking count.
    expect(screen.getByText(/1 blocking condition on this lane/i)).toBeTruthy();
    // The severity word is in the text — colour is never the only signal.
    expect(screen.getAllByText('Blocking').length).toBe(1);
    expect(screen.getAllByText('Warning').length).toBe(1);
    expect(screen.getByText('This bot’s exit has not flattened its position')).toBeTruthy();
    // Both items name a strategy on an account-confirmed lane, so both offer
    // their way in — one link per condition.
    expect(screen.getAllByRole('link', { name: 'Open bot' }).length).toBe(2);
  });

  it('renders a grey unknown — never quiet — when this lane’s read failed', async () => {
    await renderBell({ unknown: true, errorReason: 'clerk_unreachable', items: [] });

    const bell = bellButton();
    expect(bell.getAttribute('aria-label')).toContain('unknown');

    await fireEvent.click(bell);
    expect(screen.getByText(/never the same as quiet/i)).toBeTruthy();
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('shows the same eligibility wording for every notice (#2440)', async () => {
    // 2026-09-03 04:00 ET: the pre-market open after an exit that could not go out.
    const nextAttemptAtMs = 1_788_422_400_000;
    await renderBell({
      unknown: false,
      errorReason: null,
      items: [
        item({ recovery_status: { kind: 'allowed_from', reason_code: 'NO_SESSION_OPEN', explanation: 'No session is open.', allowed_from_ms: nextAttemptAtMs } }),
        item({
          condition_id: 'unc-2',
          strategy_instance_id: 'ema-2',
          recovery_status: { kind: 'allowed_from', reason_code: 'NO_SESSION_OPEN', explanation: 'No session is open.', allowed_from_ms: nextAttemptAtMs },
        }),
        item({ condition_id: 'unc-3', strategy_instance_id: 'ema-3', reason_code: 'ORDER_OUTCOME_UNKNOWN' }),
      ],
    });

    await fireEvent.click(bellButton());

    const [promised, eligible] = screen.getAllByText(/Automatic retry/);
    expect(promised.textContent).toContain('04:00');
    expect(promised.textContent).toContain('ET');
    expect(promised.textContent).toContain('Automatic retry: allowed from');
    expect(promised.textContent).not.toContain('waiting');
    expect(eligible.textContent).toContain('Automatic retry: allowed from');
    expect(eligible.textContent).not.toContain('waiting');
    expect(eligible.textContent).toContain('04:00');
    // A condition with no scheduled attempt says nothing about one.
    expect(screen.getAllByText(/Automatic retry/).length).toBe(2);
  });

  it('says an exit is working, or that the next try is unknown, as the desk does (#2440 review)', async () => {
    await renderBell({
      unknown: false,
      errorReason: null,
      items: [
        item({  recovery_status: { kind: 'working', reason_code: 'OWN_EXIT_WORKING', explanation: 'An exit is in progress.' } }),
        item({
          condition_id: 'unc-2',
          strategy_instance_id: 'ema-2',

          recovery_status: { kind: 'unknown', reason_code: 'RECOVERY_RECORD_UNREADABLE', explanation: "Recovery status is unknown; this notice's record could not be read." },
        }),
      ],
    });

    await fireEvent.click(bellButton());

    expect(
      screen.getByText('An exit is in progress.'),
    ).toBeTruthy();
    expect(
      screen.getByText("Recovery status is unknown; this notice's record could not be read."),
    ).toBeTruthy();
  });

  it('closes the popover on Escape', async () => {
    await renderBell({ unknown: false, errorReason: null, items: [item()] });
    await fireEvent.click(bellButton());
    expect(screen.getByRole('dialog')).toBeTruthy();

    await fireEvent.keyDown(bellButton(), { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('hands the keyboard back to the bell when Escape closes the popover from inside it', async () => {
    const { fixture } = await renderBell({ unknown: false, errorReason: null, items: [item()] });
    await fireEvent.click(bellButton());
    const link = screen.getByRole('dialog').querySelector('a');
    link?.focus();

    await fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    await fixture.whenStable();

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(bellButton());
  });

  it.each([
    ['closed', false],
    ['open', true],
  ])('has no detectable accessibility violations with the popover %s', async (_name, open) => {
    await renderBell({ unknown: false, errorReason: null, items: [
      item(),
      item({
        condition_id: 'stopped-holding:ema-2', kind: 'stopped_holding', reason_code: 'STOPPED_STILL_HOLDING',
        severity: 'warning', strategy_instance_id: 'ema-2', headline: 'ema-2 is stopped but still holds 5 SPY.',
        action: { label: 'Flatten…', destination: 'bot' },
      }),
    ] });
    if (open) {
      await fireEvent.click(bellButton());
      expect(screen.getByRole('dialog')).toBeTruthy();
    }

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });

    expect(results.violations).toEqual([]);
  });
});
