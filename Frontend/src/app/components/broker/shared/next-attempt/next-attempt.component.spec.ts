import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { NextAttemptComponent, type NextAttemptFacts } from './next-attempt.component';

// 2026-09-03 04:00 ET: the actual pre-market open.
const PRE_MARKET_OPEN_MS = 1_788_422_400_000;
type RecoveryStatus = NonNullable<NextAttemptFacts['recovery_status']>;

async function renderStatus(status: RecoveryStatus): Promise<HTMLElement> {
  const { fixture } = await render(NextAttemptComponent, { inputs: { facts: { recovery_status: status } } });
  return fixture.nativeElement as HTMLElement;
}

describe('NextAttemptComponent (#2504)', () => {
  it('renders the backend eligibility instant in ET', async () => {
    await renderStatus({
      kind: 'allowed_from', reason_code: 'NO_SESSION_OPEN', explanation: 'No session is open.',
      allowed_from_ms: PRE_MARKET_OPEN_MS,
    });
    const line = screen.getByText(/Automatic retry/);
    expect(line.textContent).toContain('04:00:00');
    expect(line.textContent).toContain('ET');
  });

  it.each(['working', 'on_hold', 'allowed_now', 'broker_unreachable', 'stuck', 'unknown'] as const)(
    'renders the evaluated %s status without inventing eligibility', async kind => {
      await renderStatus({ kind, reason_code: 'EXIT_SYMBOL_HALTED', explanation: `Clerk says ${kind}.` });
      expect(screen.getByText(`Clerk says ${kind}.`)).toBeTruthy();
      expect(screen.queryByText(/Automatic retry/)).toBeNull();
      expect(screen.queryByText('EXIT_SYMBOL_HALTED')).toBeNull();
    },
  );

  it('shows original exposure age separately from the latest check', async () => {
    await renderStatus({
      kind: 'working', reason_code: 'OWN_EXIT_WORKING', explanation: 'An exit is in progress.',
      stuck_since_ms: PRE_MARKET_OPEN_MS, last_checked_at_ms: PRE_MARKET_OPEN_MS + 60_000,
    });
    expect(screen.getByText(/Position still open since/)).toBeTruthy();
    expect(screen.getByText(/Last checked/)).toBeTruthy();
  });

  it('shows an unreadable record as unknown', async () => {
    await render(NextAttemptComponent, { inputs: { facts: { recovery_status: { kind: 'unknown', reason_code: 'RECOVERY_RECORD_UNREADABLE', explanation: "Recovery status is unknown; this notice's record could not be read." } } } });
    expect(screen.getByText(/Recovery status is unknown/)).toBeTruthy();
  });

  it('takes no space when no recovery is attached', async () => {
    const { fixture } = await render(NextAttemptComponent, { inputs: { facts: {} } });
    expect(fixture.nativeElement.style.display).toBe('none');
  });
});
