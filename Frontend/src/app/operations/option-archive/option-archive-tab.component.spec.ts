import { signal } from '@angular/core';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { OptionArchiveService, type OptionArchiveHealth } from '../../shared/option-archive/option-archive.service';
import { formatTimestampDisplay } from '../../shared/timestamp/timestamp-display';
import { OptionArchiveTabComponent } from './option-archive-tab.component';

function status(): OptionArchiveHealth {
  return { enabled: true, generated_ms: 1792558800000,
    underlyings: [{ underlying: 'SPY', severity: 'healthy', reason: 'Every requested session is preserved.',
      pending_sessions: 0, partial_sessions: 0, lost_sessions: 0, gaps: [], phase: 'complete',
      complete_through_session_ms: 1792503000000, last_archived_session_ms: 1792503000000,
      snapshot_missed_evenings: 0, snapshot_incomplete: 0, snapshot_field_gaps: 0 }],
    backup: { severity: 'healthy', reason: 'Generation checked.', generation_id: 'backup-a', last_success_ms: 1792558800000 },
    test_restore: { severity: 'healthy', reason: 'Restored and decoded.', last_success_ms: 1792558800000 } };
}
function world(initial: OptionArchiveHealth | null = status()) {
  return { health: signal(initial), unavailable: signal(false), retry: vi.fn() };
}
function renderTab(archive: ReturnType<typeof world>) {
  return render(OptionArchiveTabComponent, { providers: [{ provide: OptionArchiveService, useValue: archive }] });
}

describe('Operations → Option archive', () => {
  it('shows each underlying\'s coverage and leaves backup evidence to the Backups tab', async () => {
    await renderTab(world());
    expect(screen.getByRole('heading', { name: 'Option archive' })).toBeTruthy();
    expect(screen.getByText('Every requested session is preserved.')).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Lake backup' })).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Weekly restore check' })).toBeNull();
  });
  it('keeps an older hole visible behind a recent success', async () => {
    const health = status();
    health.underlyings[0] = { ...health.underlyings[0], severity: 'at_risk', partial_sessions: 2, lost_sessions: 1,
      reason: 'An older session remains partial.', gaps: [{ session_ms: 1729261800000, permanent: true,
        reason: 'The vendor no longer serves this session.', recorded_ms: 1792558800000 }], phase: 'rth_pause' };
    await renderTab(world(health));
    expect(screen.getByText('An older session remains partial.')).toBeTruthy();
    expect(screen.getByText(/The vendor no longer serves this session/)).toBeTruthy();
    expect(screen.getByText(/Capture is paused/)).toBeTruthy();
  });
  it('shows unavailable status and retry without inventing health', async () => {
    const archive = world(null); archive.unavailable.set(true);
    await renderTab(archive);
    screen.getByRole('button', { name: 'Retry status' }).click();
    expect(archive.retry).toHaveBeenCalledOnce();
    expect(screen.queryByText('Every requested session is preserved.')).toBeNull();
  });
  it('renders deadlines, failure and capture freshness from server evidence', async () => {
    const health = status();
    health.underlyings[0] = { ...health.underlyings[0], earliest_required_session_ms: 1729517400000,
      next_due_ms: 1792645200000, last_success_ms: 1792558800000, last_failure: 'A retained receipt could not be admitted.',
      gaps: [{ session_ms: 1729517400000, permanent: false, days_left: 5, reason: 'Capture pending.', recorded_ms: 1792558800000 }] };
    await renderTab(world(health));
    expect(screen.getByText('Earliest required session').nextElementSibling?.textContent?.trim())
      .toBe(formatTimestampDisplay(1729517400000, { mode: 'date-et' }));
    expect(screen.getByText('Next capture due').nextElementSibling?.textContent?.trim())
      .toBe(formatTimestampDisplay(1792645200000, { mode: 'local' }));
    expect(screen.getByText('Last successful capture').nextElementSibling?.textContent?.trim())
      .toBe(formatTimestampDisplay(1792558800000, { mode: 'local' }));
    expect(screen.getByText('A retained receipt could not be admitted.')).toBeTruthy();
    expect(screen.getByText(/5 calendar days remain/)).toBeTruthy();
  });
});
