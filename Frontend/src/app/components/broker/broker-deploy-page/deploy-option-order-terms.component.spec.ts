import { fireEvent, render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { DeployOptionOrderTermsComponent } from './deploy-option-order-terms.component';
import { startingOrderTerms, type OptionOrderTerms } from './option-structure/option-structure';
import { OPTION_CATALOGUE } from './option-structure/option-structure.fixtures';

const DEFAULTS = OPTION_CATALOGUE.order_terms;
const TERMS = startingOrderTerms(DEFAULTS);

async function renderTerms(terms: OptionOrderTerms | null = TERMS, defaults: typeof DEFAULTS | null = DEFAULTS) {
  const termsChange = vi.fn<(terms: OptionOrderTerms) => void>();
  await render(DeployOptionOrderTermsComponent, { inputs: { terms, defaults }, on: { termsChange } });
  return { termsChange };
}

function box(name: RegExp): HTMLInputElement {
  return screen.getByRole<HTMLInputElement>('spinbutton', { name });
}

describe('DeployOptionOrderTermsComponent', () => {
  it('shows each term under the catalogue’s label, with its unit and its starting value', async () => {
    await renderTerms();

    expect(screen.getByRole('group', { name: 'Option order terms · fixed for this bot’s life' })).toBeTruthy();
    const shown = screen.getAllByRole('spinbutton').map((input) => [
      input.closest('.order-terms__field')?.querySelector('label')?.textContent?.replace(/\s+/g, ' ').trim(),
      (input as HTMLInputElement).value,
      input.nextElementSibling?.textContent?.trim(),
    ]);
    expect(shown).toEqual([
      ['Widest bid-ask gap, each leg (% of mid)', '8', '% of mid'],
      ['Entry wait (seconds)', '30', 'seconds'],
      ['Whole-structure cost (% of cash set aside)', '10', '% of cash set aside'],
      ['Expiry-day close (min before close)', '45', 'min before close'],
    ]);
  });

  it('keeps a percent as the owner typed it, and a count as a whole number', async () => {
    const { termsChange } = await renderTerms();

    fireEvent.change(box(/^Whole-structure cost/), { target: { value: '7.50' } });
    expect(termsChange).toHaveBeenLastCalledWith({ ...TERMS, cost_limit_percent: '7.50' });

    fireEvent.change(box(/^Entry wait/), { target: { value: '20' } });
    expect(termsChange).toHaveBeenLastCalledWith({ ...TERMS, entry_wait_seconds: 20 });
  });

  it('holds no count for a box that is empty or no whole number, and marks what cannot be sent', async () => {
    const { termsChange } = await renderTerms({ ...TERMS, gap_limit_percent: '', expiry_close_lead_minutes: null });

    expect(box(/^Widest bid-ask gap/).getAttribute('aria-invalid')).toBe('true');
    expect(box(/^Expiry-day close/).getAttribute('aria-invalid')).toBe('true');
    expect(box(/^Entry wait/).getAttribute('aria-invalid')).toBe('false');

    fireEvent.change(box(/^Entry wait/), { target: { value: '2.5' } });
    expect(termsChange).toHaveBeenLastCalledWith({ ...TERMS, gap_limit_percent: '', expiry_close_lead_minutes: null, entry_wait_seconds: null });
  });

  it('has no accessibility violation, with a term that cannot be sent', async () => {
    await renderTerms({ ...TERMS, gap_limit_percent: '' });

    // Landmarks are the page's: on Deploy this sits inside the How step's region.
    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false }, region: { enabled: false } } });
    expect(results.violations.map((violation) => violation.id)).toEqual([]);
  });

  it('offers no box before the catalogue’s terms have been read', async () => {
    await renderTerms(null, null);

    expect(screen.queryByRole('spinbutton')).toBeNull();
    expect(screen.getByRole('status').textContent).toContain('Reading the option order terms…');
  });
});
