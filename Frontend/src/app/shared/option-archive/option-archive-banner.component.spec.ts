import { signal } from '@angular/core';
import { provideRouter } from '@angular/router';
import { render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';
import { OptionArchiveBannerComponent } from './option-archive-banner.component';
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
  return { health: signal(initial), unavailable: signal(false), retry: vi.fn() };
}

describe('option archive banner', () => {
  it('hides a healthy banner and links unhealthy coverage to its row on the Option archive tab', async () => {
    const archive = world();
    await render(OptionArchiveBannerComponent, { providers: [provideRouter([]), { provide: OptionArchiveService, useValue: archive }] });
    expect(screen.queryByRole('link')).toBeNull();
    const health = status(); health.attention = true;
    health.underlyings[0] = { ...health.underlyings[0], severity: 'at_risk' };
    archive.health.set(health);
    await waitFor(() => expect(screen.getByRole('link', { name: 'Inspect archive health' }).getAttribute('href')).toBe('/operations/option-archive#option-archive-SPY'));
  });
  it('links a backup that needs attention to the Backups tab', async () => {
    const health = status(); health.attention = true;
    health.backup = { severity: 'failed', reason: 'The configured destination is missing.' };
    await render(OptionArchiveBannerComponent, { providers: [provideRouter([]), { provide: OptionArchiveService, useValue: world(health) }] });
    expect(screen.getByRole('link', { name: 'Inspect archive health' }).getAttribute('href')).toBe('/operations/backups#option-archive-backup');
  });
  it('warns on route failure, linking to where status is retried, without owning run controls', async () => {
    const archive = world(null); archive.unavailable.set(true);
    await render(OptionArchiveBannerComponent, { providers: [provideRouter([]), { provide: OptionArchiveService, useValue: archive }] });
    expect(screen.getByRole('link', { name: 'Inspect archive health' }).getAttribute('href')).toBe('/operations/option-archive#option-archive');
    expect(screen.queryByRole('button')).toBeNull();
  });
});
