import { fireEvent, render, screen, waitFor, within } from '@testing-library/angular';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { fakeCharts, FAKE_CHART_THEME } from '../testing/fake-charts';
import { GoldenSearchChartComponent } from './golden-search-chart.component';
import type { ChartSpec } from './golden-search-chart-spec';

function chartSpec(overrides: Partial<ChartSpec> = {}): ChartSpec {
  return {
    label: 'Development cumulative return',
    summary: 'Ends at All-period fit +8.7%.',
    featured: 'all_period',
    option: (theme) => ({
      tooltip: { trigger: 'axis', formatter: () => `<b style="color: ${theme.text}">2025-12-31</b>` },
      series: [{ id: 'equity:all_period', type: 'line', data: [0, 0.04, 0.087] }],
    }),
    table: {
      caption: 'Development cumulative return at each session close',
      columns: ['Session', 'All-period fit return'],
      rows: [
        { key: '1', cells: ['2025-12-30', '+4.0%'] },
        { key: '2', cells: ['2025-12-31', '+8.7%'] },
      ],
    },
    ...overrides,
  };
}

/** Captures the host's ResizeObserver so a spec can report a panel resize. */
function captureResize(): { observed: Element[]; fire: () => void; disconnected: () => boolean } {
  const observed: Element[] = [];
  let callback: ResizeObserverCallback | null = null;
  let disconnected = false;
  vi.stubGlobal(
    'ResizeObserver',
    class {
      constructor(cb: ResizeObserverCallback) {
        callback = cb;
      }
      observe(target: Element): void {
        observed.push(target);
      }
      unobserve(): void {}
      disconnect(): void {
        disconnected = true;
      }
    },
  );
  return { observed, fire: () => callback?.([], {} as ResizeObserver), disconnected: () => disconnected };
}

describe('GoldenSearchChartComponent', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('draws the spec in the theme once the library loads, follows its panel’s size, redraws on a new spec and disposes with the view', async () => {
    const resize = captureResize();
    const fake = fakeCharts();
    const view = await render(GoldenSearchChartComponent, { inputs: { spec: chartSpec() }, providers: fake.providers });

    await waitFor(() => expect(fake.charts[0]?.options).toHaveLength(1));
    const [chart] = fake.charts;
    expect(screen.getByRole('img', { name: 'Development cumulative return. Ends at All-period fit +8.7%.' }).contains(chart.element)).toBe(true);
    const tooltip = chart.options[0].tooltip;
    const formatter = !Array.isArray(tooltip) && typeof tooltip?.formatter === 'function' ? tooltip.formatter : null;
    expect(formatter?.([], '', () => undefined)).toBe(`<b style="color: ${FAKE_CHART_THEME.text}">2025-12-31</b>`);

    expect(resize.observed).toEqual([chart.element]);
    resize.fire();
    expect(chart.resizes).toBe(1);

    view.fixture.componentRef.setInput('spec', chartSpec({ summary: 'Ends at All-period fit +9.1%.' }));
    await waitFor(() => expect(chart.options).toHaveLength(2));

    view.fixture.destroy();
    expect(chart.disposed).toBe(true);
    expect(resize.disconnected()).toBe(true);
  });

  it('Show as table lists every value the chart draws, and hides it again', async () => {
    await render(GoldenSearchChartComponent, { inputs: { spec: chartSpec() }, providers: fakeCharts().providers });

    const toggle = screen.getByRole('button', { name: 'Show as table' });
    fireEvent.click(toggle);

    const table = screen.getByRole('table', { name: 'Development cumulative return at each session close' });
    expect(within(table).getAllByRole('row').map((row) => Array.from(row.children, (cell) => cell.textContent?.trim()).join(' '))).toEqual([
      'Session All-period fit return',
      '2025-12-30 +4.0%',
      '2025-12-31 +8.7%',
    ]);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    fireEvent.click(screen.getByRole('button', { name: 'Hide the table' }));
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('a chart that cannot draw says so, clears the drawing it replaces and opens its table instead', async () => {
    const fake = fakeCharts();
    const view = await render(GoldenSearchChartComponent, { inputs: { spec: chartSpec() }, providers: fake.providers });
    await waitFor(() => expect(fake.charts[0]?.options).toHaveLength(1));

    const broken = chartSpec({
      option: () => {
        throw new Error('bad option');
      },
    });
    view.fixture.componentRef.setInput('spec', broken);

    expect((await screen.findByRole('alert')).textContent).toContain('The chart could not be drawn. Every value is in the table below.');
    expect(fake.charts[0].clears).toBe(1);
    expect(screen.getByRole('table', { name: 'Development cumulative return at each session close' })).not.toBeNull();
  });
});
