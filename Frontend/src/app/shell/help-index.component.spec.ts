import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { HELP_DOCUMENTS } from '../shared/help/help-documents';
import { HelpIndexComponent } from './help-index.component';

async function openIndex() {
  const view = await render(HelpIndexComponent);
  fireEvent.click(screen.getByRole('button', { name: 'Help documents' }));
  return view;
}

function linkOf(title: string): HTMLAnchorElement {
  const link = screen.getByText(title).closest('a');
  if (link === null) throw new Error(`${title} is not a link`);
  return link;
}

describe('HelpIndexComponent', () => {
  it('lists nothing until the question mark is pressed', async () => {
    await render(HelpIndexComponent);

    expect(screen.queryAllByRole('link')).toEqual([]);
  });

  it('lists every served document, Architecture Manual and Legal Notices included, each opening its own address in a new tab', async () => {
    await openIndex();

    expect(screen.getAllByRole('link')).toHaveLength(HELP_DOCUMENTS.length);
    for (const document of HELP_DOCUMENTS) {
      const link = linkOf(document.title);
      expect.soft(link.getAttribute('href'), `${document.title} opens ${document.route}`).toBe(document.route);
      expect.soft(link.getAttribute('target'), `${document.title} keeps the page you are on`).toBe('_blank');
    }
    expect(linkOf('Architecture Manual').getAttribute('href')).toBe('/docs/architecture-manual');
    expect(linkOf('Legal Notices').getAttribute('href')).toBe('/legal/notices');
  });

  it('closes when a document is chosen or a press lands elsewhere', async () => {
    await openIndex();
    fireEvent.click(linkOf('Broker Costs & Timing'));
    expect(screen.queryAllByRole('link')).toEqual([]);

    fireEvent.click(screen.getByRole('button', { name: 'Help documents' }));
    expect(screen.getAllByRole('link').length).toBeGreaterThan(0);
    fireEvent.mouseDown(document.body);
    expect(screen.queryAllByRole('link')).toEqual([]);
  });
});
