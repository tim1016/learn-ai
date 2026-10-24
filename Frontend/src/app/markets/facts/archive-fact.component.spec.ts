import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { formatTimestampDisplay } from '../../shared/timestamp';
import { ArchiveFactComponent } from './archive-fact.component';
import { archive } from './testing/facts-fixtures';

const text = (element: HTMLElement) => (element.textContent ?? '').replace(/\s+/g, ' ').trim();
const date = (ms: number) => formatTimestampDisplay(ms, { mode: 'date-et', granularity: 'short-date' });

describe('ArchiveFactComponent', () => {
  it('says some days are stored, and the latest, when gaps remain, and never that the archive covers anything', async () => {
    const fact = archive({ kind: 'some_days', last_archived_session_ms: Date.UTC(2026, 9, 21, 13, 30) });
    await render(ArchiveFactComponent, { inputs: { fact } });

    const badge = screen.getByText(/Options archive: some days stored/);
    expect(text(badge)).toBe(`Options archive: some days stored, latest ${date(Date.UTC(2026, 9, 21, 13, 30))}`);
    expect(badge.getAttribute('title')).toContain('4 required sessions are not');
    expect(screen.queryByText(/every required day/)).toBeNull();
  });

  it('says every required day is stored, over the archive\'s own span, only when none is missing', async () => {
    const fact = archive({
      kind: 'complete',
      earliest_required_session_ms: Date.UTC(2025, 9, 20, 13, 30),
      complete_through_session_ms: Date.UTC(2026, 9, 21, 13, 30),
      missing_sessions: 0,
    });
    await render(ArchiveFactComponent, { inputs: { fact } });

    const badge = screen.getByText(/Options archive: every required day stored/);
    expect(text(badge)).toBe(`Options archive: every required day stored, ${date(Date.UTC(2025, 9, 20, 13, 30))} to ${date(Date.UTC(2026, 9, 21, 13, 30))}`);
    expect(badge.getAttribute('title')).toContain('not a claim about a narrower window');
  });

  it('says no day is stored for a tracked underlying the archive has captured nothing of', async () => {
    await render(ArchiveFactComponent, { inputs: { fact: archive({ kind: 'none_stored', last_archived_session_ms: null, missing_sessions: 9 }) } });

    expect(screen.getByText('Options archive: no days stored')).toBeTruthy();
  });

  it('says unavailable, with the reason, when the archive\'s receipts could not be read', async () => {
    await render(ArchiveFactComponent, { inputs: { fact: archive({ kind: 'unavailable', reason: 'The option archive\'s receipts cannot be read.' }) } });

    expect(screen.getByText('Options archive: unavailable').getAttribute('title')).toBe('The option archive\'s receipts cannot be read.');
  });

  it.each([[null], [undefined]])('draws nothing for an underlying the archive does not track (%s)', async (fact) => {
    const { container } = await render(ArchiveFactComponent, { inputs: { fact } });

    expect(container.textContent?.trim()).toBe('');
  });
});
