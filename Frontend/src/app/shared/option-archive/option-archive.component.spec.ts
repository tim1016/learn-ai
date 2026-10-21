import { signal } from '@angular/core';
import { provideRouter } from '@angular/router';
import { render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';
import { OptionArchiveBannerComponent } from './option-archive-banner.component';
import { OptionArchiveCardComponent } from './option-archive-card.component';
import { formatTimestampDisplay } from '../timestamp/timestamp-display';
import { OptionArchiveService, type OptionArchiveHealth } from './option-archive.service';

function status(): OptionArchiveHealth {
  return { enabled: true, generated_ms: 1792558800000, attention: false,
    underlyings: [{ underlying: 'SPY', severity: 'healthy', reason: 'Every requested session is preserved.',
      pending_sessions: 0, partial_sessions: 0, lost_sessions: 0, gaps: [], phase: 'complete',
      complete_through_session_ms: 1792503000000, last_archived_session_ms: 1792503000000,
      snapshot_missed_evenings: 0, snapshot_incomplete: 0, snapshot_field_gaps: 0 }],
    backup: { severity: 'healthy', reason: 'Generation checked.', generation_id: 'backup-a', last_success_ms: 1792558800000 },
    test_restore: { severity: 'healthy', reason: 'Restored and decoded.', last_success_ms: 1792558800000 } };
}
function world(initial: OptionArchiveHealth | null = status()) {
  return { health: signal(initial), unavailable: signal(false), loading: signal(false), retry: vi.fn() };
}

describe('option archive health', () => {
  it('shows coverage and independent backup and restore evidence', async () => {
    const archive = world();
    await render(OptionArchiveCardComponent, { providers: [{ provide: OptionArchiveService, useValue: archive }] });
    expect(screen.getByRole('heading', { name: 'Option archive' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Lake backup' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Weekly restore check' })).toBeTruthy();
    expect(screen.getByText('Every requested session is preserved.')).toBeTruthy();
    expect(screen.getByText(/backup-a/)).toBeTruthy();
  });
  it('keeps an older hole and failed backup visible behind a recent success', async () => {
    const health = status(); health.attention = true;
    health.underlyings[0] = { ...health.underlyings[0], severity: 'at_risk', partial_sessions: 2, lost_sessions: 1,
      reason: 'An older session remains partial.', gaps: [{ session_ms: 1729261800000, permanent: true,
        reason: 'The vendor no longer serves this session.', recorded_ms: 1792558800000 }], phase: 'rth_pause' };
    health.backup = { severity: 'failed', reason: 'The configured destination is missing.' };
    await render(OptionArchiveCardComponent, { providers: [{ provide: OptionArchiveService, useValue: world(health) }] });
    expect(screen.getByText('An older session remains partial.')).toBeTruthy();
    expect(screen.getByText(/The vendor no longer serves this session/)).toBeTruthy();
    expect(screen.getByText('The configured destination is missing.')).toBeTruthy();
    expect(screen.getByText(/Capture is paused/)).toBeTruthy();
  });
  it('shows unavailable status and retry without inventing health', async () => {
    const archive = world(null); archive.unavailable.set(true);
    await render(OptionArchiveCardComponent, { providers: [{ provide: OptionArchiveService, useValue: archive }] });
    screen.getByRole('button', { name: 'Retry status' }).click();
    expect(archive.retry).toHaveBeenCalledOnce();
    expect(screen.queryByRole('heading', { name: 'Lake backup' })).toBeNull();
  });
  it('hides a healthy banner and links unhealthy coverage to the exact row', async () => {
    const archive = world();
    await render(OptionArchiveBannerComponent, { providers: [provideRouter([]), { provide: OptionArchiveService, useValue: archive }] });
    expect(screen.queryByRole('link')).toBeNull();
    const health = status(); health.attention = true;
    health.underlyings[0] = { ...health.underlyings[0], severity: 'at_risk' };
    archive.health.set(health);
    await waitFor(() => expect(screen.getByRole('link', { name: 'Inspect archive health' }).getAttribute('href')).toBe('/data-lab#option-archive-SPY'));
  });
  it('warns on route failure without owning run controls', async () => {
    const archive = world(null); archive.unavailable.set(true);
    await render(OptionArchiveBannerComponent, { providers: [provideRouter([]), { provide: OptionArchiveService, useValue: archive }] });
    expect(screen.getByRole('link', { name: 'Inspect archive health' })).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
  });
  it('renders deadlines, failure, capture and recovery freshness from server evidence', async () => {
    const health = status();
    health.underlyings[0] = { ...health.underlyings[0], earliest_required_session_ms: 1729517400000,
      next_due_ms: 1792645200000, last_success_ms: 1792558800000, last_failure: 'A retained receipt could not be admitted.',
      gaps: [{ session_ms: 1729517400000, permanent: false, days_left: 5, reason: 'Capture pending.', recorded_ms: 1792558800000 }] };
    health.backup = { ...health.backup, age_ms: 120000, oldest_unbacked_capture_ms: 1792558000000 };
    health.test_restore = { ...health.test_restore, age_ms: 180000 };
    await render(OptionArchiveCardComponent, { providers: [{ provide: OptionArchiveService, useValue: world(health) }] });
    expect(screen.getByText('Earliest required session').nextElementSibling?.textContent?.trim())
      .toBe(formatTimestampDisplay(1729517400000, { mode: 'date-et' }));
    expect(screen.getByText('Next capture due').nextElementSibling?.textContent?.trim())
      .toBe(formatTimestampDisplay(1792645200000, { mode: 'local' }));
    expect(screen.getByText('Last successful capture').nextElementSibling?.textContent?.trim())
      .toBe(formatTimestampDisplay(1792558800000, { mode: 'local' }));
    expect(screen.getByText('A retained receipt could not be admitted.')).toBeTruthy();
    expect(screen.getByText(/5 calendar days remain/)).toBeTruthy();
    expect(screen.getByText(/Backup age: 2m 00s/)).toBeTruthy();
    expect(screen.getByText(/Restore check age: 3m 00s/)).toBeTruthy();
    expect(screen.getByText(/Oldest capture awaiting backup/).textContent)
      .toContain(formatTimestampDisplay(1792558000000, { mode: 'local' }));
  });

});
