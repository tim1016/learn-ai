import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { MarkdownDocument } from './markdown-document.model';
import { DocumentArticleComponent } from './document-article.component';

describe('DocumentArticleComponent', () => {
  it('renders canonical blocks as native, navigable document elements', async () => {
    await render(DocumentArticleComponent, {
      inputs: {
        document: documentFixture,
        route: '/broker/bot-manual',
        fragment: null,
      },
      providers: [provideRouter([])],
    });

    expect(screen.getByRole('navigation', { name: 'Manual contents' })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Start safely', level: 2 })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Advanced verification', level: 5 })).toBeTruthy();
    expect(screen.getByText('Read the canonical source first.')).toBeTruthy();
    expect(screen.getByRole('separator')).toBeTruthy();
    expect(screen.getByRole('note').textContent).toContain('Read the receipt before acting.');
    const procedure = screen.getByText('Verify the account state.').closest('ol');
    expect(procedure?.textContent).toContain('Verify the account state.');
    expect(screen.getByRole('table').textContent).toContain('Single-use');

    const anchors = screen.getAllByRole('link', { name: 'Link to Start safely' });
    expect(anchors[0]?.getAttribute('href')).toBe('/broker/bot-manual#start-safely');
  });

  it('follows CSS-safe chapter fragments for numbered headings', async () => {
    const scrollIntoView = vi.fn();
    const originalScrollIntoView = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView');
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      value: scrollIntoView,
    });

    try {
      await render(DocumentArticleComponent, {
        inputs: {
          document: {
            ...documentFixture,
            sections: [{ ...documentFixture.sections[0], id: 'document-1-start-safely' }],
          },
          route: '/broker/bot-manual',
          fragment: 'document-1-start-safely',
        },
        providers: [provideRouter([])],
      });

      await Promise.resolve();
      expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'smooth', block: 'start' });
    } finally {
      if (originalScrollIntoView) {
        Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', originalScrollIntoView);
      } else {
        Reflect.deleteProperty(HTMLElement.prototype, 'scrollIntoView');
      }
    }
  });
});

const documentFixture: MarkdownDocument = {
  title: 'Operator Manual',
  preamble: [
    {
      id: 'source-note',
      kind: 'code',
      language: 'text',
      value: 'Read the canonical source first.',
    },
    { id: 'source-divider', kind: 'divider' },
  ],
  sections: [
    {
      id: 'start-safely',
      ordinal: 1,
      title: 'Start safely',
      blocks: [
        {
          id: 'advanced-verification',
          kind: 'subheading',
          level: 5,
          title: 'Advanced verification',
        },
        {
          id: 'receipt-note',
          kind: 'callout',
          html: '<p>Read the receipt before acting.</p>',
        },
        {
          id: 'procedure',
          kind: 'list',
          ordered: true,
          start: 1,
          items: [{ id: 'verify-state', html: '<p>Verify the account state.</p>' }],
        },
        {
          id: 'gates',
          kind: 'table',
          headers: [
            { id: 'gate-header', html: 'Gate', alignment: 'left' },
            { id: 'rule-header', html: 'Rule', alignment: 'left' },
          ],
          rows: [
            {
              id: 'roll-call',
              cells: [
                { id: 'gate', html: 'Roll call', alignment: 'left' },
                { id: 'rule', html: 'Single-use', alignment: 'left' },
              ],
            },
          ],
        },
      ],
    },
  ],
};
