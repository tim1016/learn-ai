import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { HowThisWorksLinkComponent } from './how-this-works-link.component';

describe('HowThisWorksLinkComponent', () => {
  it('opens the document at its address and section in a new tab, leaving the page where it is', async () => {
    await render(HowThisWorksLinkComponent, {
      inputs: { route: '/docs/indicator-reliability-methodology', anchor: '1-context-and-scope' },
    });
    const link = screen.getByRole('link', { name: 'How this works' });

    expect(link.getAttribute('href')).toBe('/docs/indicator-reliability-methodology#1-context-and-scope');
    expect(link.getAttribute('target')).toBe('_blank');
    expect(link.getAttribute('rel')).toContain('noopener');
  });

  it('carries the query the document reads and the words a page gives it', async () => {
    await render(HowThisWorksLinkComponent, {
      inputs: { route: '/docs/golden-search-manual', query: { study: 'study-0001', unset: undefined }, label: 'Operating manual' },
    });

    expect(screen.getByRole('link', { name: 'Operating manual' }).getAttribute('href')).toBe(
      '/docs/golden-search-manual?study=study-0001',
    );
  });

  it('opens the bare address while the page has no query to carry', async () => {
    await render(HowThisWorksLinkComponent, {
      inputs: { route: '/docs/golden-search-manual', query: { study: undefined } },
    });

    expect(screen.getByRole('link', { name: 'How this works' }).getAttribute('href')).toBe('/docs/golden-search-manual');
  });
});
