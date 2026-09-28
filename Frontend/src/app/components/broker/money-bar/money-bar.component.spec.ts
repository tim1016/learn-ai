import { render, screen, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { fakeAccountMoney } from '../../../testing/account-money-fixtures';
import type { MoneySegment } from '../v2-panel/lib/broker-v2-panel.service';
import { MoneyBarComponent, type MoneyBarSize } from './money-bar.component';

const SEGMENTS = fakeAccountMoney().segments ?? [];

/** Rendered where a page puts it — inside a landmark — so AXE grades the bar
 * itself rather than a harness with no page around it. */
async function renderBar(size: MoneyBarSize = 'home', segments: readonly MoneySegment[] = SEGMENTS) {
  return render(
    '<main aria-label="Home"><app-money-bar [segments]="segments" [size]="size" caption="Where this account’s money is" /></main>',
    { imports: [MoneyBarComponent], componentProperties: { segments, size } },
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
      'held by stopped bot spy-ema-20260925-1402 $670.43',
      'account charges $0.01',
      'free to deploy $98,329.57',
    ]);
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
