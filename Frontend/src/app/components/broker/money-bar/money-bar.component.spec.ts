import { render, screen, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { fakeAccountMoney } from '../../../testing/account-money-fixtures';
import { BOT_HUES } from '../v2-panel/lib/bot-hue';
import type { MoneySegment } from '../v2-panel/lib/broker-v2-panel.service';
import { MoneyBarComponent, type MoneyBarNotes, type MoneyBarSize } from './money-bar.component';

const SEGMENTS = fakeAccountMoney().segments ?? [];

/** Rendered where a page puts it — inside a landmark — so AXE grades the bar
 * itself rather than a harness with no page around it. */
async function renderBar(
  size: MoneyBarSize = 'home',
  segments: readonly MoneySegment[] = SEGMENTS,
  notes: MoneyBarNotes | null = null,
) {
  return render(
    '<main aria-label="Home"><app-money-bar [segments]="segments" [size]="size" [notes]="notes" caption="Where this account’s money is" /></main>',
    { imports: [MoneyBarComponent], componentProperties: { segments, size, notes } },
  );
}

function slices(container: Element): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>('.money-bar__track > .money-bar__slice'));
}

describe('MoneyBarComponent', () => {
  it('names every slice with its Python-authored amount in the legend', async () => {
    await renderBar();

    const legend = screen.getByRole('list', { name: 'Where this account’s money is' });
    const entries = within(legend).getAllByRole('listitem').map((item) => item.textContent?.replace(/\s+/g, ' ').trim());
    expect(entries).toEqual([
      'spy-ema-20260929-0931 $999.99 in shares $764.71 · in entry orders $0.00 · free $235.28',
      'held by stopped bot spy-ema-20260925-1402 $670.43 released $0.00 · still claimed $0.00',
      'account charges $0.01',
      'free to deploy $98,329.57',
    ]);
  });

  it('states a bot’s shortfall and what a stopped bot released and still claims', async () => {
    const [bot, stopped] = SEGMENTS;
    await renderBar('detail', [
      { ...bot, shortfall_usd: '41.37' },
      { ...stopped, released_usd: '329.57', still_claimed_usd: '12.05' },
    ]);

    expect(screen.getByText(/short of next entry \$41\.37/)).toBeTruthy();
    expect(screen.getByText(/released \$329\.57 · still claimed \$12\.05/)).toBeTruthy();
  });

  it('never states a shortfall the backend did not send', async () => {
    await renderBar('detail', [{ ...SEGMENTS[0], shortfall_usd: null }]);

    expect(screen.queryByText(/short of next entry/)).toBeNull();
  });

  it('draws an empty bot slice as no part at all, and still names its overrun', async () => {
    const [bot] = SEGMENTS;
    const { container } = await renderBar('detail', [
      {
        ...bot, amount_usd: '0.00', share_bps: 0, shortfall_usd: '5.01',
        parts: { in_shares_usd: '0.00', in_shares_bps: 0, pending_usd: '0.00', pending_bps: 0, free_usd: '0.00', free_bps: 0 },
      },
      { kind: 'free', label: 'free to deploy', amount_usd: '994.99', share_bps: 10_000 },
    ]);

    expect(slices(container).map((slice) => slice.className)).toEqual(['money-bar__slice money-bar__slice--free']);
    expect(container.querySelectorAll('.money-bar__part')).toHaveLength(0);
    expect(screen.getByText(/short of next entry \$5\.01/)).toBeTruthy();
  });

  it('draws money settling into cash with its own treatment and names it', async () => {
    const { container } = await renderBar('detail', [
      SEGMENTS[0],
      { kind: 'settling', label: 'settling into cash', amount_usd: '1234.50', share_bps: 123 },
      { kind: 'free', label: 'free to deploy', amount_usd: '97095.07', share_bps: 9777 },
    ]);

    expect(slices(container).map((slice) => slice.className)).toContain('money-bar__slice money-bar__slice--settling');
    const legend = screen.getByRole('list', { name: 'Where this account’s money is' });
    expect(within(legend).getByText('settling into cash')).toBeTruthy();
    expect(within(legend).getByText('$1,234.50')).toBeTruthy();
    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations).toEqual([]);
  });

  it('colours a bot by its backend palette slot, whatever its place on this bar', async () => {
    const { container } = await renderBar('detail', [
      { ...SEGMENTS[0], strategy_instance_id: 'later', label: 'later', palette_index: 3 },
      { ...SEGMENTS[0], strategy_instance_id: 'first', label: 'first', palette_index: 0 },
    ]);

    expect(slices(container).map((slice) => slice.style.getPropertyValue('--money-bar-hue'))).toEqual([BOT_HUES[3], BOT_HUES[0]]);
  });

  it('states the account’s notes beside the bar, each only when the backend sent it', async () => {
    const view = fakeAccountMoney();
    const { rerender } = await renderBar('home', SEGMENTS, view);

    expect(screen.getByText('Open P&L $12.40')).toBeTruthy();
    expect(screen.queryByText(/Claims exceed the account/)).toBeNull();

    const why = 'Open P&L needs a current price for every holding, and this account’s prices are not part of this read.';
    await rerender({
      componentProperties: {
        segments: SEGMENTS, size: 'home',
        notes: { account_shortfall_usd: '50.00', open_pnl_usd: null, open_pnl_detail: why },
      },
    });

    expect(screen.getByText(/Claims exceed the account by \$50\.00/)).toBeTruthy();
    expect(screen.getByText(why)).toBeTruthy();
    expect(screen.queryByText(/Open P&L \$/)).toBeNull();
  });

  it('draws each slice at the backend’s basis points, with no arithmetic of its own', async () => {
    const { container } = await renderBar();

    const drawn = slices(container);
    expect(drawn.map((slice) => slice.style.flexGrow)).toEqual(['100', '67', '1', '9832']);
    expect(drawn.map((slice) => slice.className)).toEqual([
      'money-bar__slice money-bar__slice--bot',
      'money-bar__slice money-bar__slice--stopped',
      'money-bar__slice money-bar__slice--charges',
      'money-bar__slice money-bar__slice--free',
    ]);
    const parts = Array.from(drawn[0].querySelectorAll<HTMLElement>('.money-bar__part'));
    expect(parts.map((part) => [part.className, part.style.flexGrow])).toEqual([
      ['money-bar__part money-bar__part--shares', '7647'],
      ['money-bar__part money-bar__part--pending', '0'],
      ['money-bar__part money-bar__part--free', '2353'],
    ]);
  });

  it('draws a NEW slice with its own treatment, carved from free to deploy', async () => {
    const withNew: MoneySegment[] = [
      SEGMENTS[0],
      { kind: 'new', label: 'new bot', amount_usd: '800.00', share_bps: 80 },
      { kind: 'free', label: 'free to deploy', amount_usd: '98199.99', share_bps: 9820 },
    ];
    const { container } = await renderBar('detail', withNew);

    expect(slices(container).map((slice) => slice.className)).toContain('money-bar__slice money-bar__slice--new');
    expect(screen.getByText('new bot')).toBeTruthy();
    expect(screen.getByText('$800.00')).toBeTruthy();
  });

  it('keeps the legend for assistive technology at the small sizes', async () => {
    const { container } = await renderBar('mini');

    const legend = screen.getByRole('list', { name: 'Where this account’s money is' });
    expect(legend.classList.contains('money-bar__legend--hidden')).toBe(true);
    expect(within(legend).getByText('free to deploy')).toBeTruthy();
    expect(container.querySelector('.money-bar__track')?.getAttribute('aria-hidden')).toBe('true');
  });

  it.each<MoneyBarSize>(['home', 'detail', 'row', 'mini'])('has no detectable accessibility violations at %s size', async (size) => {
    await renderBar(size);

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });

    expect(results.violations).toEqual([]);
  });
});
