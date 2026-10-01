import { fireEvent, render, screen, within } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { GoldenSearchPairMapComponent } from './golden-search-pair-map.component';
import type { PairMap } from './golden-search.types';
import { emaCapability, metrics, pairMap } from './testing/fixtures';

const CENTER = { gap: 0.15, rsi_min: 48, rsi_max: 72, fast_period: 8, slow_period: 21, hold_bars: 4, symbol: 'SPY' };

async function renderMap(maps: PairMap[] = [pairMap()]) {
  return render(GoldenSearchPairMapComponent, {
    inputs: { maps, capability: emaCapability(), center: CENTER, centerLabel: 'the All-period fit', headingId: 'gs-map' },
  });
}

function grid(): HTMLElement {
  return screen.getByRole('group', { name: /parameter map: fast ema length × slow ema length/i });
}

function percentOf(background: string): number {
  const match = /(\d+)%/.exec(background);
  return match === null ? -1 : Number(match[1]);
}

describe('GoldenSearchPairMapComponent', () => {
  it('lays the landscape out with the first knob down and the second across, and opens on the candidate’s own cell', async () => {
    await renderMap();

    expect(screen.getByText('Fast EMA length ↓ / Slow EMA length →')).not.toBeNull();
    expect(screen.getByText('Both in decision bars')).not.toBeNull();
    const own = within(grid()).getByRole('button', { name: 'Fast EMA length 8, Slow EMA length 21, development net return +8.7%' });
    expect(own.getAttribute('aria-pressed')).toBe('true');
    expect(own.textContent?.trim()).toBe('+8.7%');
    expect(screen.getByRole('status').textContent).toContain('Fast EMA length 8 · Slow EMA length 21 · +8.7% net return · Sharpe 1.18');
    expect(screen.getByText(/held fixed: crossover gap 0\.15 · rsi lower gate 48 · rsi upper gate 72 · hold time 4/i)).not.toBeNull();
    expect(screen.getByText(/cannot establish robustness/i)).not.toBeNull();
  });

  it('names an invalid pair instead of showing a number, and never offers it as a choice', async () => {
    await renderMap();

    const invalid = within(grid()).getByRole('img', { name: /invalid: fast ema length 15, slow ema length 10 — the fast ema must be shorter/i });
    expect(invalid.textContent?.trim()).toBe('—');
    expect(within(grid()).getAllByRole('button')).toHaveLength(21);
    expect(within(grid()).getAllByRole('img', { name: /^invalid/i })).toHaveLength(4);
  });

  it('selecting a cell describes it; a failed run says it failed, with its error', async () => {
    await renderMap();

    fireEvent.click(within(grid()).getByRole('button', { name: 'Fast EMA length 5, Slow EMA length 15, development net return +2.4%' }));
    expect(screen.getByRole('status').textContent).toContain('Fast EMA length 5 · Slow EMA length 15 · +2.4% net return');
    expect(within(grid()).getByRole('button', { name: /fast ema length 8, slow ema length 21/i }).getAttribute('aria-pressed')).toBe('false');

    fireEvent.click(within(grid()).getByRole('button', { name: 'Fast EMA length 13, Slow EMA length 34, the run failed' }));
    expect(screen.getByRole('status').textContent).toContain('the run failed: engine refused the window');
  });

  it('fills a higher return deeper, and a loss in the warning tint', async () => {
    await renderMap();

    const best = within(grid()).getByRole('button', { name: /fast ema length 8, slow ema length 21,/i });
    const weak = within(grid()).getByRole('button', { name: /fast ema length 5, slow ema length 15,/i });
    const loss = within(grid()).getByRole('button', { name: /fast ema length 5, slow ema length 10,/i });
    expect(percentOf(best.style.background)).toBeGreaterThan(percentOf(weak.style.background));
    expect(best.style.background).toContain('--bull');
    expect(loss.style.background).toContain('--warn');
  });

  it('moves focus between cells with the arrow keys, skipping invalid pairs', async () => {
    await renderMap();
    const start = within(grid()).getByRole('button', { name: /fast ema length 13, slow ema length 15,/i });
    start.focus();

    fireEvent.keyDown(start, { key: 'ArrowLeft' });
    expect(document.activeElement).toBe(start);
    fireEvent.keyDown(start, { key: 'ArrowUp' });
    expect(document.activeElement?.getAttribute('aria-label')).toMatch(/fast ema length 10, slow ema length 15,/i);
    fireEvent.keyDown(document.activeElement ?? start, { key: 'ArrowRight' });
    expect(document.activeElement?.getAttribute('aria-label')).toMatch(/fast ema length 10, slow ema length 21,/i);
  });

  it('gives the same values as a table, and switches between pair landscapes', async () => {
    const rsi: PairMap = {
      x_knob: 'rsi_max',
      y_knob: 'rsi_min',
      x_values: [65, 72],
      y_values: [48, 70],
      cells: [
        { x: 65, y: 48, status: 'tested', metrics: metrics({ total_return_pct: 0.03 }), reason: null },
        { x: 72, y: 48, status: 'tested', metrics: metrics({ total_return_pct: 0.087 }), reason: null },
        { x: 65, y: 70, status: 'invalid', metrics: null, reason: 'The lower RSI gate must be below the upper gate.' },
        { x: 72, y: 70, status: 'untested', metrics: null, reason: null },
      ],
    };
    await renderMap([pairMap(), rsi]);

    const table = screen.getByRole('table', { name: /development net return — rows: fast ema length/i });
    expect(within(table).getAllByRole('row')[2].textContent).toMatch(/8\s*\+1\.9%\s*\+6\.8%\s*\+8\.7%/);
    expect(table.textContent).toContain('Invalid');
    expect(table.textContent).toContain('Run failed');

    fireEvent.click(screen.getByRole('button', { name: 'RSI lower gate × RSI upper gate' }));

    expect(screen.getByRole('group', { name: /parameter map: rsi lower gate × rsi upper gate/i })).not.toBeNull();
    expect(screen.getByRole('status').textContent).toContain('RSI lower gate 48 · RSI upper gate 72 · +8.7% net return');
    expect(screen.getByRole('img', { name: 'Not tested: RSI lower gate 70, RSI upper gate 72' })).not.toBeNull();
  });

  it('passes axe', async () => {
    const view = await renderMap();

    const results = await axe.run(view.container, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`)).toEqual([]);
  });
});
