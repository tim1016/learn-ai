import { provideRouter } from '@angular/router';
import { render, screen, waitFor } from '@testing-library/angular';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { HealthNoticeComponent } from './health-notice.component';
import { SCOPED_HEALTH_REFRESH_MS } from './scoped-health.service';
import { allHealthy, fakeScopedHealth, healthItem, projection } from './testing/scoped-health-fixtures';

async function renderNotice(
  answer: Parameters<typeof fakeScopedHealth>[0],
  inputs: { needs?: readonly ('options_history' | 'recovery' | 'stock_history' | 'live_chain')[]; instrument?: string | null; note?: string | null } = {},
) {
  const fake = fakeScopedHealth(answer);
  const view = await render(HealthNoticeComponent, {
    inputs: { needs: inputs.needs ?? ['options_history', 'recovery'], instrument: inputs.instrument ?? null, note: inputs.note ?? null },
    providers: [provideRouter([]), fake.provider],
  });
  return { ...view, ...fake };
}

afterEach(() => vi.useRealTimers());

describe('the health notice', () => {
  it('asks the data plane about what the page does, and for its one symbol when it reads one', async () => {
    const { read } = await renderNotice(allHealthy(), { needs: ['stock_history'], instrument: 'SPY' });

    await waitFor(() => expect(read).toHaveBeenCalledWith({ operations: ['stock_history'], instrument: 'SPY' }));
  });

  it('says nothing while it waits, and nothing when what the page depends on is healthy', async () => {
    const { read, container, fixture } = await renderNotice(allHealthy());

    expect(container.textContent?.trim()).toBe('');
    await waitFor(() => expect(read).toHaveBeenCalledOnce());
    await fixture.whenStable();
    expect(container.textContent?.trim()).toBe('');
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('does not make a notice of a finding the data plane marks as informational', async () => {
    const { read, container, fixture } = await renderNotice(
      projection(healthItem({ kind: 'option_archive', instrument: 'SPY', severity: 'info', explanation: 'SPY: catching up.' })),
    );

    await waitFor(() => expect(read).toHaveBeenCalledOnce());
    await fixture.whenStable();
    expect(container.textContent).not.toContain('catching up');
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('links an archive finding to its card on the Option archive tab', async () => {
    await renderNotice(
      projection(
        healthItem({
          kind: 'option_archive',
          instrument: 'SPY',
          severity: 'warning',
          explanation: 'SPY: Archive gaps need attention before their vendor deadline.',
          link: { tab: 'option-archive', query: {}, fragment: 'option-archive-SPY' },
        }),
      ),
      { note: 'Your requested sessions are checked separately by Check data.' },
    );

    const notice = await screen.findByRole('status');
    expect(notice.textContent).toContain('Needs attention.');
    expect(notice.textContent).toContain('SPY: Archive gaps need attention before their vendor deadline.');
    expect(notice.textContent).toContain('Your requested sessions are checked separately by Check data.');
    expect(screen.getByRole('link', { name: 'Open Option archive' }).getAttribute('href')).toBe('/operations/option-archive#option-archive-SPY');
  });

  it('links a lake finding to the Lake tab filtered to the symbol and view it is about', async () => {
    await renderNotice(
      projection(
        healthItem({
          kind: 'stock_bars',
          instrument: 'MSFT',
          severity: 'warning',
          link: { tab: 'lake', query: { symbol: 'MSFT', price_adjustment_mode: 'polygon_split_adjusted', data_type: 'trade' } },
        }),
      ),
      { needs: ['stock_history'], instrument: 'MSFT' },
    );

    const link = await screen.findByRole('link', { name: 'Open Lake' });
    expect(link.getAttribute('href')).toBe('/operations/lake?symbol=MSFT&price_adjustment_mode=polygon_split_adjusted&data_type=trade');
  });

  it('shows a failed backup as the backup problem it is, linked to the Backups card', async () => {
    await renderNotice(
      projection(
        healthItem({
          kind: 'lake_backup',
          severity: 'critical',
          explanation: 'The destination is unavailable. This concerns recoverability only: stored prices, quotes and charts are not marked invalid by it.',
          link: { tab: 'backups', query: {}, fragment: 'option-archive-backup' },
        }),
      ),
    );

    const notice = await screen.findByRole('status');
    expect(notice.textContent).toContain('Failing.');
    expect(notice.textContent).toContain('recoverability only');
    expect(screen.getByRole('link', { name: 'Open Backups' }).getAttribute('href')).toBe('/operations/backups#option-archive-backup');
  });

  it('shows the feed finding without a link where no Operations tab holds the evidence', async () => {
    await renderNotice(
      projection(healthItem({ kind: 'ibkr_market_data', severity: 'critical', explanation: 'The IBKR session is down. Live option chain and bot feed readiness are affected; archived history is not.' })),
      { needs: ['live_chain'] },
    );

    expect((await screen.findByRole('status')).textContent).toContain('archived history is not');
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('reads an unknown finding as unknown, never as healthy', async () => {
    await renderNotice(
      projection(healthItem({ kind: 'option_archive', severity: 'unknown', explanation: 'The option archive status could not be read.', link: { tab: 'option-archive', query: {}, fragment: 'option-archive' } })),
    );

    const notice = await screen.findByRole('status');
    expect(notice.textContent).toContain('Unknown.');
    expect(notice.textContent).not.toMatch(/\bOK\b|healthy/i);
  });

  it('leads with the worst finding and counts the others', async () => {
    await renderNotice(
      projection(
        healthItem({ kind: 'lake_backup', severity: 'critical', explanation: 'Backup failed.', link: { tab: 'backups', query: {} } }),
        healthItem({ kind: 'option_archive', instrument: 'SPY', severity: 'warning', explanation: 'SPY gaps.' }),
        healthItem({ kind: 'restore_check', severity: 'unknown', explanation: 'No restore recorded.' }),
      ),
    );

    const notice = await screen.findByRole('status');
    expect(notice.textContent).toContain('Backup failed.');
    expect(notice.textContent).not.toContain('SPY gaps.');
    expect(notice.textContent).toContain('2 more in Operations.');
  });

  it('reads a projection it cannot get as unknown, with a way to Operations', async () => {
    await renderNotice(new Error('network down'));

    const notice = await screen.findByRole('status');
    expect(notice.textContent).toContain('Unknown.');
    expect(notice.textContent).toContain('cannot be confirmed healthy');
    expect(screen.getByRole('link', { name: 'Open Operations' }).getAttribute('href')).toBe('/operations');
  });

  it('asks again when the symbol changes, and does not show the last symbol\'s finding meanwhile', async () => {
    const fake = fakeScopedHealth((query) =>
      projection(healthItem({ kind: 'stock_bars', instrument: query.instrument ?? undefined, severity: 'warning', explanation: `${query.instrument} has gaps.` })),
    );
    const { fixture } = await render(HealthNoticeComponent, {
      inputs: { needs: ['stock_history'], instrument: 'SPY' },
      providers: [provideRouter([]), fake.provider],
    });
    await screen.findByText(/SPY has gaps/);

    fixture.componentRef.setInput('instrument', 'MSFT');
    fixture.detectChanges();

    await screen.findByText(/MSFT has gaps/);
    expect(screen.queryByText(/SPY has gaps/)).toBeNull();
    expect(fake.read).toHaveBeenLastCalledWith({ operations: ['stock_history'], instrument: 'MSFT' });
  });

  it('reads again on the chips\' cadence, so a finding that has cleared stops being shown and one that has appeared is', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    let feedDown = true;
    const fake = fakeScopedHealth(() =>
      feedDown
        ? projection(healthItem({ kind: 'ibkr_market_data', severity: 'critical', explanation: 'The IBKR session is down.' }))
        : allHealthy(),
    );
    await render(HealthNoticeComponent, {
      inputs: { needs: ['live_chain'], instrument: null, note: null },
      providers: [provideRouter([]), fake.provider],
    });
    await screen.findByText(/The IBKR session is down/);

    feedDown = false;
    await vi.advanceTimersByTimeAsync(SCOPED_HEALTH_REFRESH_MS);
    await waitFor(() => expect(screen.queryByRole('status')).toBeNull());

    feedDown = true;
    await vi.advanceTimersByTimeAsync(SCOPED_HEALTH_REFRESH_MS);
    await screen.findByText(/The IBKR session is down/);
    expect(fake.read).toHaveBeenCalledTimes(3);
  });

  it('turns its last finding unknown when a refresh fails, rather than leaving it standing', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    let failing = false;
    const fake = fakeScopedHealth(() => {
      if (failing) throw new Error('network down');
      return projection(healthItem({ kind: 'lake_backup', severity: 'critical', explanation: 'Backup failed.' }));
    });
    await render(HealthNoticeComponent, {
      inputs: { needs: ['recovery'], instrument: null, note: null },
      providers: [provideRouter([]), fake.provider],
    });
    await screen.findByText(/Backup failed/);

    failing = true;
    await vi.advanceTimersByTimeAsync(SCOPED_HEALTH_REFRESH_MS);

    await screen.findByText(/cannot be confirmed healthy/);
    expect(screen.queryByText(/Backup failed/)).toBeNull();
  });
});
