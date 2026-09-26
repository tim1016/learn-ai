import { provideRouter } from '@angular/router';
import { render, screen, fireEvent } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { testLane } from '../fleet/fleet-directory-testing';
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
    kind: 'uncertainty',
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
  it('offers the backend Flatten action for an unmanaged position', async () => {
    await renderBell({ unknown: false, errorReason: null, items: [item({
      kind: 'position_unmanaged', reason_code: 'POSITION_UNMANAGED', severity: 'warning',
      headline: 'Bot is not managing this position', action_label: 'Flatten',
    })] });
    await fireEvent.click(bellButton());
    expect(screen.getByText('Bot is not managing this position')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Flatten' })).toBeTruthy();
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
});
