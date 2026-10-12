import { render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { reads } from '../../testing/page-actions';
import { buildTimelinePlan, NO_REASON_GIVEN, UNPLACEABLE } from '../timeline-plan';
import { CALL_585, PUT_585, SCENE_LEVELS, SERIES_WORDS, sessionDetail, shownSeries, withheldSeries } from '../testing/detail-fixtures';
import { TimelineLegendComponent } from './timeline-legend.component';

async function renderLegend(plan = buildTimelinePlan(sessionDetail())) {
  return render(TimelineLegendComponent, { inputs: { plan } });
}

describe('the legend of the chart', () => {
  it('names the underlying through the shared symbol renderer and each contract through the shared contract renderer', async () => {
    const { container } = await renderLegend();

    const panes = [...container.querySelectorAll('.panes > li')];
    expect(panes.length).toBe(3);
    expect(panes[0].querySelector('app-asset-identity')).not.toBeNull();
    expect(panes[0].querySelector('app-option-contract')).toBeNull();
    expect(reads(panes[1].querySelector('app-option-contract'))).toContain("Oct 16 '26 585 Call");
    expect(reads(panes[2].querySelector('app-option-contract'))).toContain("Oct 16 '26 585 Put");
  });

  it('draws a swatch of the line’s own style in the colour of its token, and a glyph in the mark’s colour', async () => {
    const { container } = await renderLegend();

    const swatches = [...container.querySelectorAll<HTMLElement>('.swatch')].map((each) => [each.getAttribute('data-line'), each.style.borderTopColor]);
    expect(swatches).toEqual([
      ['solid', 'var(--chart-series-blue)'],
      ['solid', 'var(--chart-series-sky)'],
      ['dotted', 'var(--chart-series-amber)'],
      ['dashed', 'var(--chart-series-violet)'],
      ['large-dashed', 'var(--chart-series-red)'],
      ['solid', 'var(--chart-series-sky)'],
    ]);
    const glyphs = [...container.querySelectorAll<HTMLElement>('.glyph')].map((each) => [reads(each), each.style.color]);
    expect(glyphs).toEqual([
      ['▲', 'var(--chart-series-green)'],
      ['▼', 'var(--chart-series-orange)'],
      ['●', 'var(--chart-series-sky)'],
      ['▲', 'var(--chart-series-green)'],
      ['■', 'var(--chart-series-purple)'],
    ]);
  });

  it('hides every swatch and glyph from a screen reader: the words beside each carry the meaning', async () => {
    const { container } = await renderLegend();

    const decorations = [...container.querySelectorAll('.swatch, .glyph')];
    expect(decorations.length).toBe(11);
    expect(decorations.every((each) => each.getAttribute('aria-hidden') === 'true')).toBe(true);
  });

  it('says nothing about what is not drawn when everything is', async () => {
    await renderLegend();

    expect(screen.queryByRole('region', { name: 'Not drawn' })).toBeNull();
  });
});

describe('what the chart does not draw', () => {
  it('gives the server’s words and its reason for a series it withheld, and points to the lists below', async () => {
    await renderLegend(buildTimelinePlan(sessionDetail({ underlying: withheldSeries('SPY', null, 'lake_unavailable'), contracts: [shownSeries(CALL_585.symbol, CALL_585, [[2, '2.00'], [3, '2.10']]), withheldSeries(PUT_585.symbol, PUT_585, 'gone')] })));

    const items = within(screen.getByRole('region', { name: 'Not drawn' })).getAllByRole('listitem');
    expect(items.map((item) => reads(item))).toEqual([
      `SPY : Lake Unavailable ${SERIES_WORDS.lake_unavailable}`,
      `SPY Oct 16 '26 585 Put : Gone ${SERIES_WORDS.gone} Its levels, fills and marks are listed below.`,
    ]);
  });

  it('gives the page’s own sentence, with no reason, for what it could not place', async () => {
    await renderLegend(buildTimelinePlan(sessionDetail({ levels: [{ ...SCENE_LEVELS[2], price_usd: 'x' }] })));

    const items = within(screen.getByRole('region', { name: 'Not drawn' })).getAllByRole('listitem');
    expect(items.map((item) => reads(item))).toEqual([`Stop level of SPY Oct 16 '26 585 Call : ${UNPLACEABLE}`]);
  });

  it('names a withheld underlying through the shared symbol renderer, and keeps plain code for a contract symbol the server sent no parts for', async () => {
    const { container } = await renderLegend(buildTimelinePlan(sessionDetail({
      underlying: withheldSeries('SPY', null, 'gone'),
      contracts: [withheldSeries(CALL_585.symbol, null, 'gone'), shownSeries(PUT_585.symbol, PUT_585, [[3, '1.40'], [4, '1.44']])],
    })));

    const items = [...container.querySelectorAll('.missing li')];
    expect(items.length).toBe(2);
    expect(items[0].querySelector('app-asset-identity')).not.toBeNull();
    expect(items[0].querySelector('code')).toBeNull();
    expect(items[1].querySelector('app-asset-identity')).toBeNull();
    expect(reads(items[1].querySelector('code'))).toBe(CALL_585.symbol);
  });

  it('never leaves the words of a withheld series empty: the page says that the server gave none', async () => {
    await renderLegend(buildTimelinePlan(sessionDetail({ underlying: { ...withheldSeries('SPY', null, 'gone'), message: null } })));

    const items = within(screen.getByRole('region', { name: 'Not drawn' })).getAllByRole('listitem');
    expect(items.map((item) => reads(item))).toEqual([`SPY : Gone ${NO_REASON_GIVEN}`]);
  });
});
