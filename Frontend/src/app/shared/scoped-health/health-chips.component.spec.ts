import { provideRouter } from '@angular/router';
import { render, screen, waitFor, within } from '@testing-library/angular';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { formatTimestampDisplay } from '../timestamp/timestamp-display';
import { HealthChipsComponent } from './health-chips.component';
import { allHealthy, fakeScopedHealth, healthItem, OBSERVED_MS, projection } from './testing/scoped-health-fixtures';

async function renderChips(answer: Parameters<typeof fakeScopedHealth>[0]) {
  const fake = fakeScopedHealth(answer);
  const view = await render(HealthChipsComponent, { providers: [provideRouter([]), fake.provider] });
  return { ...view, ...fake };
}

/** The chip for one source, read as text: its name and its state. */
async function chip(label: string): Promise<HTMLElement> {
  const items = await screen.findAllByRole('listitem');
  const found = items.find((item) => within(item).queryByText(label) !== null);
  if (found === undefined) throw new Error(`No chip for ${label}`);
  return found;
}

/** What a chip says, part by part: its source, its state and how many more findings want attention. */
function says(element: HTMLElement): string {
  return [...element.querySelectorAll('.health-chips__chip > span')].map((part) => part.textContent?.trim()).join(' ');
}

afterEach(() => vi.useRealTimers());

describe('the Operations health chips', () => {
  it('shows one chip per source, in order, each with its state', async () => {
    await renderChips(allHealthy());

    await waitFor(() => expect(screen.getAllByRole('listitem')).toHaveLength(4));
    expect(screen.getAllByRole('listitem').map((item) => says(item))).toEqual([
      'Lake OK',
      'Option archive OK',
      'Backups OK',
      'IBKR feed OK',
    ]);
    expect(screen.getAllByRole('listitem').map((item) => (item.querySelector('a, span.health-chips__chip') as HTMLElement).dataset['severity'])).toEqual(['ok', 'ok', 'ok', 'ok']);
  });

  it('shows each source by its worst finding and counts the other findings that want attention', async () => {
    await renderChips(
      projection(
        healthItem({ kind: 'stock_bars', instrument: 'MSFT', severity: 'warning', link: { tab: 'lake', query: { symbol: 'MSFT' } } }),
        healthItem({ kind: 'stock_bars', instrument: 'GLD', severity: 'warning' }),
        healthItem({ kind: 'stock_bars', instrument: 'AMZN', severity: 'warning' }),
        healthItem({ kind: 'option_archive', instrument: 'SPY', severity: 'ok' }),
        healthItem({ kind: 'lake_backup', severity: 'critical', link: { tab: 'backups', query: {}, fragment: 'option-archive-backup' } }),
        healthItem({ kind: 'restore_check', severity: 'ok' }),
        healthItem({ kind: 'ibkr_market_data', severity: 'ok' }),
      ),
    );

    expect(says(await chip('Lake'))).toBe('Lake Needs attention +2');
    expect(says(await chip('Backups'))).toBe('Backups Failing');
    expect(says(await chip('Option archive'))).toBe('Option archive OK');
  });

  it('links a chip to the evidence of its finding, and leaves the feed chip unlinked', async () => {
    await renderChips(
      projection(
        healthItem({ kind: 'stock_bars', instrument: 'MSFT', severity: 'warning', link: { tab: 'lake', query: { symbol: 'MSFT', data_type: 'trade' } } }),
        healthItem({ kind: 'option_archive', instrument: 'SPY', severity: 'ok', link: { tab: 'option-archive', query: {}, fragment: 'option-archive-SPY' } }),
        healthItem({ kind: 'lake_backup', severity: 'critical', link: { tab: 'backups', query: {}, fragment: 'option-archive-backup' } }),
        healthItem({ kind: 'ibkr_market_data', severity: 'critical' }),
      ),
    );

    expect((await chip('Lake')).querySelector('a')?.getAttribute('href')).toBe('/operations/lake?symbol=MSFT&data_type=trade');
    expect((await chip('Option archive')).querySelector('a')?.getAttribute('href')).toBe('/operations/option-archive#option-archive-SPY');
    expect((await chip('Backups')).querySelector('a')?.getAttribute('href')).toBe('/operations/backups#option-archive-backup');
    expect((await chip('IBKR feed')).querySelector('a')).toBeNull();
  });

  it('says when each source was read, in the shared time format', async () => {
    await renderChips(projection(healthItem({ kind: 'stock_bars', severity: 'ok', summary: 'Lake readable' })));

    const title = (await chip('Lake')).querySelector('.health-chips__chip')?.getAttribute('title');
    expect(title).toBe(`Lake readable (read ${formatTimestampDisplay(OBSERVED_MS, { mode: 'local' })})`);
  });

  it('reads a source the data plane did not report as unknown, not as healthy', async () => {
    await renderChips(projection(healthItem({ kind: 'stock_bars', severity: 'ok' })));

    await waitFor(() => expect(screen.getAllByRole('listitem')).toHaveLength(4));
    for (const label of ['Option archive', 'Backups', 'IBKR feed']) {
      expect(says(await chip(label))).toBe(`${label} Unknown`);
    }
    expect(says(await chip('Lake'))).toBe('Lake OK');
  });

  it('reads an unknown finding as unknown', async () => {
    await renderChips(projection(...allHealthy().items.filter((item) => item.source !== 'ibkr_feed'), healthItem({ kind: 'ibkr_market_data', severity: 'unknown' })));

    expect(says(await chip('IBKR feed'))).toBe('IBKR feed Unknown');
    expect(says(await chip('Lake'))).toBe('Lake OK');
  });

  it('reads as unknown, with no OK anywhere, when the projection cannot be read', async () => {
    const { container } = await renderChips(new Error('network down'));

    await screen.findByText('Unknown');
    expect(screen.getAllByRole('listitem')).toHaveLength(1);
    expect(container.textContent).not.toMatch(/\bOK\b/);
  });

  it('reads the projection again on a timer, and a read that fails turns the last good answer unknown', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    let failing = false;
    const fake = fakeScopedHealth(() => {
      if (failing) throw new Error('network down');
      return allHealthy();
    });
    await render(HealthChipsComponent, { providers: [provideRouter([]), fake.provider] });
    await screen.findAllByText('OK');
    expect(fake.read).toHaveBeenCalledTimes(1);

    failing = true;
    await vi.advanceTimersByTimeAsync(30_000);

    await screen.findByText('Unknown');
    expect(fake.read).toHaveBeenCalledTimes(2);
    expect(screen.queryByText('OK')).toBeNull();
  });
});
