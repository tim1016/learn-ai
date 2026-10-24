import { provideRouter } from '@angular/router';
import { render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { StudyListComponent } from './study-list.component';

async function renderList() {
  await render(StudyListComponent, { providers: [provideRouter([])] });
}

describe('StudyListComponent', () => {
  it.each([
    ['Feature study', '/research/feature', 'Does this indicator predict the next move for this instrument?'],
    ['Cross-sectional study', '/research/cross-sectional', 'Does an option-implied feature predict what a stock does next, across a whole universe?'],
    ['Signal study', '/research/signal', 'Does a trading signal built on this indicator hold up out of sample?'],
    ['Indicator reliability', '/research/indicator-reliability', 'Is this indicator’s link to what price does next real, or what chance alone produces?'],
    ['Realized vs implied vol', '/research/realized-vs-iv', 'Is implied volatility richer or cheaper than the volatility the stock then realizes?'],
  ])('lists %s with its question and a link to its study', async (title, address, question) => {
    await renderList();

    expect(screen.getByRole('link', { name: title }).getAttribute('href')).toBe(address);
    expect(screen.getByText(question)).toBeTruthy();
  });

  it('names where each study reads today, so a result is never taken for lake data', async () => {
    await renderList();

    const studies = within(screen.getByRole('list', { name: 'Studies' }));
    expect(studies.getAllByText(/^Reads Polygon/).length, 'the studies still on the provider').toBe(3);
    expect(studies.getAllByText('Reads Data lake, Polygon').length, 'the Feature study reads the lake').toBe(1);
    expect(
      studies.getAllByText('Reads Data lake, Polygon, IV recorder (recorded IV30), Polygon option snapshot (current IV marker)').length,
      'the Realized vs IV study reads its bars from the lake, its IV from the recorder',
    ).toBe(1);
  });

  it('lists the Rule sandbox beside the studies, at its own page under Research', async () => {
    await renderList();

    expect(screen.getByRole('link', { name: 'Rule sandbox' }).getAttribute('href')).toBe('/research/rule-sandbox');
  });

  it('files the Edge demonstrations under sample data, apart from the studies', async () => {
    await renderList();

    const section = screen.getByRole('region', { name: /Demonstrations/ });
    expect(within(section).getByText('sample data')).toBeTruthy();
    for (const [title, address] of [
      ['Cross-asset demo', '/research/demos/cross-asset'],
      ['Regimes demo', '/research/demos/regimes'],
      ['Edge score demo', '/research/demos/edge-score'],
      ['Trade simulator demo', '/research/demos/trade-simulator'],
    ]) {
      expect(within(section).getByRole('link', { name: title }).getAttribute('href'), title).toBe(address);
    }
  });

  it('links the saved results to the one History that indexes them all', async () => {
    await renderList();

    const section = screen.getByRole('region', { name: 'Saved results' });
    expect(within(section).getByRole('link', { name: 'History' }).getAttribute('href')).toBe('/research/history');
  });

  it('has nothing to run: opening Research starts no job', async () => {
    await renderList();

    expect(screen.queryByRole('button')).toBeNull();
  });
});
