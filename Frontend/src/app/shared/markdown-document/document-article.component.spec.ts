import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

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
    expect(screen.getByRole('note').textContent).toContain('Read the receipt before acting.');
    const procedure = screen.getByText('Verify the account state.').closest('ol');
    expect(procedure?.textContent).toContain('Verify the account state.');
    expect(screen.getByRole('table').textContent).toContain('Single-use');

    const anchors = screen.getAllByRole('link', { name: 'Link to Start safely' });
    expect(anchors[0]?.getAttribute('href')).toBe('/broker/bot-manual#start-safely');
  });
});

const documentFixture: MarkdownDocument = {
  title: 'Operator Manual',
  preamble: [],
  sections: [
    {
      id: 'start-safely',
      ordinal: 1,
      title: 'Start safely',
      blocks: [
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
