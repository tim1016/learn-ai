import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { ManualContentsComponent } from './manual-contents.component';

describe('ManualContentsComponent', () => {
  it('finds topics in descriptions, requires all search words, and recovers from no matches', async () => {
    await render(ManualContentsComponent, {
      inputs: {
        route: '/docs/example',
        chapters: [
          { id: 'costs', title: 'Understand your costs', description: 'Estimated option fees and slippage.', group: 'Before trading' },
          { id: 'timing', title: 'Read the clock', description: 'Option expiry deadlines.', group: 'During trading' },
        ],
      },
      providers: [provideRouter([])],
    });
    const search = screen.getByRole('searchbox', { name: 'Find a chapter' });
    await userEvent.type(search, 'OPTION fees');
    expect(screen.getByRole('link', { name: /Understand your costs/ }).getAttribute('href')).toBe('/docs/example#costs');
    expect(screen.queryByRole('link', { name: /Read the clock/ })).toBeNull();
    await userEvent.type(search, ' unknown');
    expect(screen.getByRole('status').textContent).toContain('No chapter matches');
    expect(screen.queryByRole('link')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Show all chapters' }));
    expect(screen.getAllByRole('link')).toHaveLength(2);
    expect(screen.queryByRole('status')).toBeNull();
  });
});
