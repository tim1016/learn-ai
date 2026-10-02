import { Component } from '@angular/core';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { fakeCharts, type FakeChart } from '../testing/fake-charts';
import { GoldenSearchChartComponent } from './golden-search-chart.component';
import { CHART_GUIDES } from './golden-search-chart-guides';
import type { ChartSpec } from './golden-search-chart-spec';
import { GoldenSearchPanelComponent } from './golden-search-panel.component';

const SPEC: ChartSpec = {
  label: 'Development cumulative return and fall from peak',
  summary: 'Ends at All-period fit +8.7%, Recent fit +11.6%.',
  featured: 'all_period',
  option: () => ({
    series: [
      { id: 'equity:recent', type: 'line', data: [0, 0.05, 0.116, '-'] },
      { id: 'equity:all_period', type: 'line', data: [0, 0.03, 0.06, 0.087] },
      { id: 'fall:recent', type: 'line', data: [0, -0.02, 0, '-'] },
      { id: 'fall:all_period', type: 'line', data: [0, -0.04, -0.01, 0] },
    ],
  }),
  table: { caption: 'values', columns: ['Session'], rows: [] },
};

@Component({
  imports: [GoldenSearchChartComponent, GoldenSearchPanelComponent],
  template: `<app-golden-search-panel chart="equity-and-fall"><app-golden-search-chart [spec]="spec" /></app-golden-search-panel>`,
})
class PanelHost {
  readonly spec = SPEC;
}

const STEPS = CHART_GUIDES['equity-and-fall'].steps;

async function openWalkthrough(): Promise<{ chart: FakeChart; callout: HTMLElement; walk: HTMLElement }> {
  const fake = fakeCharts();
  await render(PanelHost, { providers: fake.providers });
  await waitFor(() => expect(fake.charts[0]?.options).toHaveLength(1));
  const walk = screen.getByRole('button', { name: 'Walk me through it' });
  fireEvent.click(walk);
  const callout = await screen.findByRole('group', { name: 'Walkthrough of Equity and fall from peak' });
  return { chart: fake.charts[0], callout, walk };
}

/** The actions one step sent: everything after the step's opening `downplay`. */
function lastStep(chart: FakeChart): unknown[] {
  const start = chart.actions.map((action) => action.type).lastIndexOf('downplay');
  return chart.actions.slice(start);
}

describe('GoldenSearchWalkthroughComponent in a chart panel', () => {
  it('steps through the guide’s reading steps, lighting up each step’s target on the chart', async () => {
    const { chart, callout } = await openWalkthrough();

    await waitFor(() => expect(document.activeElement).toBe(callout));
    expect(callout.textContent).toContain('Step 1 of 5');
    expect(callout.textContent).toContain(STEPS[0].text);
    // The right-edge labels: the featured line's last session, tooltip open on every candidate there.
    await waitFor(() =>
      expect(lastStep(chart)).toEqual([{ type: 'downplay' }, { type: 'hideTip' }, { type: 'highlight', seriesIndex: [0, 1] }, { type: 'showTip', seriesIndex: 1, dataIndex: 3 }]),
    );

    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(callout.textContent).toContain(STEPS[1].text);
    expect(lastStep(chart)).toEqual([{ type: 'downplay' }, { type: 'hideTip' }, { type: 'highlight', seriesIndex: [0, 1] }]);

    fireEvent.keyDown(callout, { key: 'ArrowRight' });
    expect(callout.textContent).toContain('Step 3 of 5');
    expect(lastStep(chart)).toEqual([{ type: 'downplay' }, { type: 'hideTip' }, { type: 'highlight', seriesIndex: [2, 3] }]);

    fireEvent.keyDown(callout, { key: 'ArrowRight' });
    // The featured line's deepest close-to-close fall.
    expect(lastStep(chart)).toEqual([{ type: 'downplay' }, { type: 'hideTip' }, { type: 'highlight', seriesIndex: [2, 3] }, { type: 'showTip', seriesIndex: 3, dataIndex: 1 }]);

    fireEvent.keyDown(callout, { key: 'ArrowLeft' });
    expect(callout.textContent).toContain('Step 3 of 5');
  });

  it('Esc leaves the walkthrough, clears the highlight and returns focus to the button that opened it', async () => {
    const { chart, callout, walk } = await openWalkthrough();

    fireEvent.keyDown(callout, { key: 'Escape' });

    expect(screen.queryByRole('group', { name: /walkthrough/i })).toBeNull();
    expect(chart.actions.slice(-2)).toEqual([{ type: 'downplay' }, { type: 'hideTip' }]);
    expect(document.activeElement).toBe(walk);
  });

  it('Back waits on the first step, and the last step’s Done ends the walkthrough', async () => {
    const { callout, walk } = await openWalkthrough();

    expect(screen.getByRole('button', { name: 'Back' })).toHaveProperty('disabled', true);
    for (let i = 1; i < STEPS.length; i++) fireEvent.keyDown(callout, { key: 'ArrowRight' });
    expect(callout.textContent).toContain(`Step ${STEPS.length} of ${STEPS.length}`);
    fireEvent.keyDown(callout, { key: 'ArrowRight' });
    expect(callout.textContent).toContain(`Step ${STEPS.length} of ${STEPS.length}`);

    fireEvent.click(screen.getByRole('button', { name: 'Done' }));

    expect(screen.queryByRole('group', { name: /walkthrough/i })).toBeNull();
    expect(document.activeElement).toBe(walk);
  });
});
