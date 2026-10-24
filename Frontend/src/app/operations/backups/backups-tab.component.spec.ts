import { signal } from '@angular/core';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { OptionArchiveService, type OptionArchiveHealth } from '../../shared/option-archive/option-archive.service';
import { formatTimestampDisplay } from '../../shared/timestamp/timestamp-display';
import { BackupsTabComponent } from './backups-tab.component';

function status(): OptionArchiveHealth {
  return { enabled: true, generated_ms: 1792558800000, attention: false,
    underlyings: [{ underlying: 'SPY', severity: 'healthy', reason: 'Every requested session is preserved.',
      pending_sessions: 0, partial_sessions: 0, lost_sessions: 0, gaps: [], phase: 'complete',
      snapshot_missed_evenings: 0, snapshot_incomplete: 0, snapshot_field_gaps: 0 }],
    backup: { severity: 'healthy', reason: 'Generation checked.', generation_id: 'backup-a', last_success_ms: 1792558800000 },
    test_restore: { severity: 'healthy', reason: 'Restored and decoded.', last_success_ms: 1792558800000 } };
}
function world(initial: OptionArchiveHealth | null = status()) {
  return { health: signal(initial), unavailable: signal(false), retry: vi.fn() };
}
function renderTab(archive: ReturnType<typeof world>) {
  return render(BackupsTabComponent, { providers: [{ provide: OptionArchiveService, useValue: archive }] });
}

describe('Operations → Backups', () => {
  it('shows the backup and the weekly restore check as independent evidence', async () => {
    await renderTab(world());
    expect(screen.getByRole('heading', { name: 'Lake backup' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Weekly restore check' })).toBeTruthy();
    expect(screen.getByText(/backup-a/)).toBeTruthy();
    expect(screen.getByText('Restored and decoded.')).toBeTruthy();
    expect(screen.queryByText('Every requested session is preserved.')).toBeNull();
  });
  it('keeps a failed backup visible and dates the oldest capture awaiting one', async () => {
    const health = status(); health.attention = true;
    health.backup = { severity: 'failed', reason: 'The configured destination is missing.', oldest_unbacked_capture_ms: 1792558000000 };
    await renderTab(world(health));
    expect(screen.getByText('The configured destination is missing.')).toBeTruthy();
    expect(screen.getByText(/Oldest capture awaiting backup/).textContent)
      .toContain(formatTimestampDisplay(1792558000000, { mode: 'local' }));
  });
  it('leaves destination space out when the backup recorded none', async () => {
    await renderTab(world());
    expect(screen.queryByText(/Destination free space/)).toBeNull();
  });
  it('states destination space when the backup recorded it', async () => {
    const health = status(); health.backup = { ...health.backup, free_bytes: 1_500_000 };
    await renderTab(world(health));
    expect(screen.getByText('Destination free space: 1500000 bytes')).toBeTruthy();
  });
  it('shows unavailable status and retry without inventing health', async () => {
    const archive = world(null); archive.unavailable.set(true);
    await renderTab(archive);
    screen.getByRole('button', { name: 'Retry status' }).click();
    expect(archive.retry).toHaveBeenCalledOnce();
    expect(screen.queryByRole('heading', { name: 'Lake backup' })).toBeNull();
  });
});
