import { fireEvent, render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { DeployOptionOrderTermsComponent } from './deploy-option-order-terms.component';
import { orderTermsSet, startingOrderTerms, type OptionOrderTerms } from './option-structure/option-structure';
import { OPTION_CATALOGUE } from './option-structure/option-structure.fixtures';

const DEFAULTS = OPTION_CATALOGUE.order_terms;
const TERMS = startingOrderTerms(DEFAULTS);

async function renderTerms(
  terms: OptionOrderTerms | null = TERMS, defaults: typeof DEFAULTS | null = DEFAULTS, failure: string | null = null,
) {
  const termsChange = vi.fn<(terms: OptionOrderTerms) => void>();
  const { fixture } = await render(DeployOptionOrderTermsComponent, { inputs: { terms, defaults, failure }, on: { termsChange } });
  /** The terms as the form last handed them over. */
  const held = (): OptionOrderTerms => {
    const last = termsChange.mock.calls.at(-1);
    if (last === undefined) throw new Error('No terms were handed over.');
    return last[0];
  };
  return { fixture, termsChange, held };
}

function box(name: RegExp): HTMLInputElement {
  return screen.getByLabelText<HTMLInputElement>(name);
}

function boxes(): HTMLInputElement[] {
  return Array.from(document.querySelectorAll<HTMLInputElement>('.term-fields__field input'));
}

/** Type `text` into a box and leave it, as an owner does. */
function typeAndLeave(name: RegExp, text: string): void {
  fireEvent.input(box(name), { target: { value: text } });
  fireEvent.blur(box(name));
}

/** What a box says is wrong with it, in words tied to the box; `null` when it says nothing. */
function saidOf(name: RegExp): string | null {
  const described = box(name).getAttribute('aria-describedby');
  return described === null ? null : document.getElementById(described)?.textContent?.trim() ?? null;
}

describe('DeployOptionOrderTermsComponent', () => {
  it('shows each term under the catalogue’s label, with its unit and its starting value', async () => {
    await renderTerms();

    expect(screen.getByRole('group', { name: 'Option order terms · fixed for this bot’s life' })).toBeTruthy();
    const shown = boxes().map((input) => [
      input.closest('.term-fields__field')?.querySelector('label')?.textContent?.replace(/\s+/g, ' ').trim(),
      input.value,
      input.nextElementSibling?.textContent?.trim(),
    ]);
    expect(shown).toEqual([
      ['Widest bid-ask gap, each leg (% of mid)', '8', '% of mid'],
      ['Entry wait (seconds)', '20', 'seconds'],
      ['Whole-structure cost (% of cash set aside)', '10', '% of cash set aside'],
      ['Expiry-day close (min before close)', '45', 'min before close'],
    ]);
  });

  it('keeps a percent as the owner typed it, and a count as a number', async () => {
    const { held } = await renderTerms();

    typeAndLeave(/^Whole-structure cost/, '7.50');
    expect(held()).toEqual({ ...TERMS, cost_limit_percent: '7.50' });

    typeAndLeave(/^Entry wait/, '20');
    expect(held()).toEqual({ ...TERMS, cost_limit_percent: '7.50', entry_wait_seconds: 20 });
    expect(orderTermsSet(held(), DEFAULTS)).toBe(true);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it.each(['.3', '0.000001', '1e1', '100'])('takes a percent the server takes: %s', async (percent) => {
    const { fixture, held } = await renderTerms();

    typeAndLeave(/^Widest bid-ask gap/, percent);
    await fixture.whenStable();

    expect(held().gap_limit_percent).toBe(percent);
    expect(orderTermsSet(held(), DEFAULTS)).toBe(true);
    expect(saidOf(/^Widest bid-ask gap/)).toBeNull();
    expect(box(/^Widest bid-ask gap/).getAttribute('aria-invalid')).toBe('false');
  });

  // Each refusal is the catalogue's own sentence for the box's range: the form holds no bound and no words of its own.
  const GAP = `Enter ${DEFAULTS.gap_limit_percent.bounds.sentence}.`;
  const COST = `Enter ${DEFAULTS.cost_limit_percent.bounds.sentence}.`;
  const WAIT = `Enter ${DEFAULTS.entry_wait_seconds.bounds.sentence}.`;
  const CLOSE = `Enter ${DEFAULTS.expiry_close_lead_minutes.bounds.sentence}.`;
  it.each([
    [/^Widest bid-ask gap/, '0', GAP],
    [/^Widest bid-ask gap/, '-1', GAP],
    [/^Widest bid-ask gap/, '100.5', GAP],
    [/^Widest bid-ask gap/, '0.0000001', GAP],
    [/^Widest bid-ask gap/, '', GAP],
    [/^Widest bid-ask gap/, 'eight', GAP],
    [/^Whole-structure cost/, '-1', COST],
    [/^Whole-structure cost/, '101', COST],
    [/^Entry wait/, '2.5', WAIT],
    [/^Entry wait/, '0', WAIT],
    [/^Entry wait/, '31', WAIT],
    [/^Expiry-day close/, '', CLOSE],
    [/^Expiry-day close/, '391', CLOSE],
  ])('says in words why %s cannot be “%s”, and the terms are not set', async (name, typed, why) => {
    const { fixture, held } = await renderTerms();

    typeAndLeave(name, typed);
    await fixture.whenStable();

    expect(orderTermsSet(held(), DEFAULTS)).toBe(false);
    expect(saidOf(name)).toContain(why);
    expect(box(name).getAttribute('aria-invalid')).toBe('true');
    // Every other box is left alone.
    expect(screen.getAllByRole('alert')).toHaveLength(1);
  });

  it('says the server’s reason for the entry wait’s range with its refusal, and of no other term', async () => {
    const { fixture } = await renderTerms();

    typeAndLeave(/^Entry wait/, '31');
    typeAndLeave(/^Expiry-day close/, '391');
    await fixture.whenStable();

    const reason = DEFAULTS.entry_wait_seconds.range_reason ?? '';
    expect(reason).not.toBe('');
    expect(saidOf(/^Entry wait/)).toContain(reason);
    expect(saidOf(/^Expiry-day close/)).toBe(CLOSE);
  });

  it('says under the boxes how every option bot’s orders go, in the server’s four sentences', async () => {
    const { fixture } = await renderTerms();
    fixture.componentRef.setInput('notes', OPTION_CATALOGUE.order_term_notes);
    await fixture.whenStable();

    const group = screen.getByRole('group', { name: /^Option order terms/ });
    for (const sentence of Object.values(OPTION_CATALOGUE.order_term_notes)) expect(group.textContent).toContain(sentence);
  });

  it('opens what each term does and the range it takes, in the server’s words', async () => {
    await renderTerms();

    expect(screen.getByRole('button', { name: 'What each order term does' })).toBeTruthy();
    const help = document.getElementById('deploy-option-order-terms-help');
    expect(help?.textContent).toContain(DEFAULTS.entry_wait_seconds.help);
    expect(help?.textContent).toContain(DEFAULTS.entry_wait_seconds.bounds.sentence);
    expect(help?.textContent).toContain(DEFAULTS.gap_limit_percent.help);
  });

  it('takes a cost limit of nothing, which the widest gap may not be', async () => {
    const { fixture, held } = await renderTerms();

    typeAndLeave(/^Whole-structure cost/, '0');
    await fixture.whenStable();

    expect(orderTermsSet(held(), DEFAULTS)).toBe(true);
    expect(saidOf(/^Whole-structure cost/)).toBeNull();
  });

  it('says nothing of a box while the owner is still in it', async () => {
    const { fixture } = await renderTerms();

    fireEvent.input(box(/^Widest bid-ask gap/), { target: { value: '0' } });
    await fixture.whenStable();

    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('has no accessibility violation, with a term that cannot be sent', async () => {
    const { fixture } = await renderTerms();
    typeAndLeave(/^Widest bid-ask gap/, '0');
    await fixture.whenStable();

    // Landmarks are the page's: on Deploy this sits inside the How step's region.
    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false }, region: { enabled: false } } });
    expect(results.violations.map((violation) => violation.id)).toEqual([]);
  });

  it('offers no box before the catalogue’s terms have been read', async () => {
    await renderTerms(null, null);

    expect(boxes()).toEqual([]);
    expect(screen.getByRole('status').textContent).toContain('Reading the option order terms…');
  });

  it('says why the terms could not be read once the read has failed, and no longer that it is reading', async () => {
    await renderTerms(null, null, 'Stand-in: the catalogue could not be read.');

    expect(boxes()).toEqual([]);
    expect(screen.queryByText(/Reading the option order terms/)).toBeNull();
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.getByRole('group', { name: /^Option order terms/ }).textContent).toContain('Stand-in: the catalogue could not be read.');
  });
});
