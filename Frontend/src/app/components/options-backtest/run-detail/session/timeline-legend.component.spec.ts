import { render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { reads } from '../../testing/page-actions';
import { buildTimelinePlan, UNPLACEABLE } from '../timeline-plan';
import { CALL_585, PUT_585, SCENE_LEVELS, SERIES_WORDS, sessionDetail, shownSeries, withheldSeries } from '../testing/detail-fixtures';
import { TimelineLegendComponent } from './timeline-legend.component';

async function renderLegend(plan = buildTimelinePlan(sessionDetail())) {
  return render(TimelineLegendComponent, { inputs: { plan } });
}

describe('the legend of the chart', () => {
  it('names the underlying and each contract through the shared renderers, draws a swatch and a glyph for each line and mark, and says nothing of what is not drawn when everything is', async () => {
    const { container } = await renderLegend();

    const panes = [...container.querySelectorAll('.panes > li')];
    expect.soft(panes.length, 'names the underlying through the shared symbol renderer and each contract through the shared contract renderer: one pane each').toBe(3);
    expect.soft(panes[0].querySelector('app-asset-identity'), 'names the underlying through the shared symbol renderer').not.toBeNull();
    expect.soft(panes[0].querySelector('app-option-contract'), 'names no contract in the underlying’s pane').toBeNull();
    expect.soft(reads(panes[1].querySelector('app-option-contract')), 'names the call through the shared contract renderer').toContain("Call 585 Oct 16 '26");
    expect.soft(reads(panes[2].querySelector('app-option-contract')), 'names the put through the shared contract renderer').toContain("Put 585 Oct 16 '26");

    const swatches = [...container.querySelectorAll<HTMLElement>('.swatch')].map((each) => [each.getAttribute('data-line'), each.style.borderTopColor]);
    expect.soft(swatches, 'draws a swatch of the line’s own style in the colour of its token').toEqual([
      ['solid', 'var(--chart-series-blue)'],
      ['solid', 'var(--chart-series-sky)'],
      ['dotted', 'var(--chart-series-amber)'],
      ['dashed', 'var(--chart-series-violet)'],
      ['large-dashed', 'var(--chart-series-red)'],
      ['solid', 'var(--chart-series-sky)'],
    ]);
    const glyphs = [...container.querySelectorAll<HTMLElement>('.glyph')].map((each) => [reads(each), each.style.color]);
    expect.soft(glyphs, 'draws a glyph in the mark’s colour').toEqual([
      ['▲', 'var(--chart-series-green)'],
      ['▼', 'var(--chart-series-orange)'],
      ['●', 'var(--chart-series-sky)'],
      ['▲', 'var(--chart-series-green)'],
      ['■', 'var(--chart-series-purple)'],
    ]);

    const decorations = [...container.querySelectorAll('.swatch, .glyph')];
    expect.soft(decorations.length, 'draws a swatch or a glyph beside each line and mark').toBe(11);
    expect.soft(decorations.every((each) => each.getAttribute('aria-hidden') === 'true'), 'hides every swatch and glyph from a screen reader: the words beside each carry the meaning').toBe(true);
    expect.soft(screen.queryByRole('region', { name: 'Not drawn' }), 'says nothing about what is not drawn when everything is').toBeNull();
  });
});

describe('what the chart does not draw', () => {
  it('gives the server’s words and its reason for a series it withheld, and points to the lists below', async () => {
    await renderLegend(buildTimelinePlan(sessionDetail({ underlying: withheldSeries('SPY', null, 'lake_unavailable'), contracts: [shownSeries(CALL_585.symbol, CALL_585, [[2, '2.00'], [3, '2.10']]), withheldSeries(PUT_585.symbol, PUT_585, 'gone')] })));

    const items = within(screen.getByRole('region', { name: 'Not drawn' })).getAllByRole('listitem');
    expect(items.map((item) => reads(item))).toEqual([
      `SPY : Lake Unavailable ${SERIES_WORDS.lake_unavailable}`,
      `SPY Put 585 Oct 16 '26 : Gone ${SERIES_WORDS.gone} Its levels, fills and marks are listed below.`,
    ]);
  });

  it('gives the page’s own sentence, with no reason, for what it could not place', async () => {
    await renderLegend(buildTimelinePlan(sessionDetail({ levels: [{ ...SCENE_LEVELS[2], price_usd: 'x' }] })));

    const items = within(screen.getByRole('region', { name: 'Not drawn' })).getAllByRole('listitem');
    expect(items.map((item) => reads(item))).toEqual([`Stop level of SPY Call 585 Oct 16 '26 : ${UNPLACEABLE}`]);
  });

  it('names a withheld underlying and decodes an option symbol even without separate contract parts', async () => {
    const { container } = await renderLegend(buildTimelinePlan(sessionDetail({
      underlying: withheldSeries('SPY', null, 'gone'),
      contracts: [withheldSeries(CALL_585.symbol, null, 'gone'), shownSeries(PUT_585.symbol, PUT_585, [[3, '1.40'], [4, '1.44']])],
    })));

    const items = [...container.querySelectorAll('.missing li')];
    expect(items.length).toBe(2);
    expect(items[0].querySelector('app-asset-identity')).not.toBeNull();
    expect(items[0].querySelector('code')).toBeNull();
    expect(items[1].querySelector('.option-chip')?.getAttribute('aria-label')).toBe("SPY Oct 16 '26 585 Call");
  });

});
