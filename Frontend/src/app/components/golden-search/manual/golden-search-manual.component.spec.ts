import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { expect, it } from 'vitest';
import { GoldenSearchManualComponent } from './golden-search-manual.component';

it('carries the study into every workflow link and the presentation report', async () => {
  const view = await render(GoldenSearchManualComponent, { inputs: { study: 'study-a' }, providers: [provideRouter([])] });
  const steps = screen.getAllByRole('link', { name: 'Open this step in your study' });
  expect(steps).toHaveLength(5);
  expect(steps.map(link => link.getAttribute('href'))).toEqual(['plan', 'search', 'test', 'compare', 'decision'].map(step => `/golden-search/study-a?step=${step}`));
  expect(screen.getByRole('link', { name: 'Open your presentation report' }).getAttribute('href')).toBe('/golden-search/study-a?view=report');
  const result = await axe.run(view.container, { rules: { 'color-contrast': { enabled: false } } });
  expect(result.violations).toEqual([]);
});
