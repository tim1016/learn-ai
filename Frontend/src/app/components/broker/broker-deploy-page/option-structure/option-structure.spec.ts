import { describe, expect, it } from 'vitest';

import { quoteAgeSeconds } from './option-preview-display';
import {
  addedRow,
  legRow,
  legsInWords,
  legsOf,
  onExpiryRule,
  onStrikeRule,
  orderTermsInWords,
  orderTermsSet,
  previewNotAsked,
  previewRequest,
  startingOrderTerms,
  structureNamed,
  withoutRow,
  type LegRow,
  type OptionLegRules,
} from './option-structure';
import { BULL_CALL_SPREAD_LEGS, OPTION_CATALOGUE } from './option-structure.fixtures';

const RULES = OPTION_CATALOGUE;
const [DELTA, STEPS, DISTANCE] = OPTION_CATALOGUE.strike_rules;
const [AT_LEAST, , , FIXED_DATE] = OPTION_CATALOGUE.expiry_rules;

function rowsOf(legs: readonly OptionLegRules[]): LegRow[] {
  return legs.map((leg) => legRow(leg, RULES.strike_rules));
}

/** The rows with one box of one row changed. */
function withRow(rows: readonly LegRow[], index: number, change: (row: LegRow) => LegRow): LegRow[] {
  return rows.map((row, at) => (at === index ? change(row) : row));
}

describe('the legs of an option structure', () => {
  it('names a structure only while its legs are the catalogue’s starting legs for it', () => {
    expect(structureNamed(BULL_CALL_SPREAD_LEGS, OPTION_CATALOGUE)).toBe('bull_call_spread');
    expect(structureNamed([BULL_CALL_SPREAD_LEGS[0]], OPTION_CATALOGUE)).toBe('long_call');
    expect(structureNamed([], OPTION_CATALOGUE)).toBeNull();

    const edited = legsOf(withRow(rowsOf(BULL_CALL_SPREAD_LEGS), 0, (row) => ({ ...row, strike: { ...row.strike, value: 0.45 } })), RULES);
    expect(edited).not.toBeNull();
    expect(structureNamed(edited ?? [], OPTION_CATALOGUE)).toBeNull();
  });

  it('reads a leg that leaves its tolerance to the rule as the same leg', () => {
    const [bought, sold] = BULL_CALL_SPREAD_LEGS;
    const terse: OptionLegRules[] = [
      { ...bought, strike: { selector: 'delta', target: 0.5 } },
      { ...sold, strike: { selector: 'leg_distance', leg_id: 'long_call', dollars: 5 } },
    ];

    expect(structureNamed(terse, OPTION_CATALOGUE)).toBe('bull_call_spread');
    expect(rowsOf(terse)[0].strike.tolerance).toBe(0.05);
  });

  it.each([
    ['an empty number', (row: LegRow): LegRow => ({ ...row, strike: { ...row.strike, value: null } })],
    ['an empty “Within” on a rule that has one', (row: LegRow): LegRow => ({ ...row, strike: { ...row.strike, tolerance: null } })],
    ['a quantity that is no whole number', (row: LegRow): LegRow => ({ ...row, qty_ratio: 1.5 })],
    ['no quantity', (row: LegRow): LegRow => ({ ...row, qty_ratio: null })],
    ['a count of days that is no whole number', (row: LegRow): LegRow => ({ ...onExpiryRule(row, AT_LEAST), expiry: { selector: 'min_dte', days: 2.5, date: '' } })],
    ['a fixed date not picked yet', (row: LegRow): LegRow => onExpiryRule(row, FIXED_DATE)],
    ['a rule just chosen, its number not typed', (row: LegRow): LegRow => onStrikeRule(row, STEPS, [])],
  ])('holds no legs while a row has %s', (_what, change) => {
    expect(legsOf(withRow(rowsOf(BULL_CALL_SPREAD_LEGS), 0, change), RULES)).toBeNull();
  });

  it('sends a fixed date as its 16:00 New York instant, and reads it back as the date', () => {
    const rows = withRow(rowsOf([BULL_CALL_SPREAD_LEGS[0]]), 0, (row) => ({ ...row, expiry: { selector: 'absolute', days: null, date: '2026-10-09' } }));

    const legs = legsOf(rows, RULES);

    expect(legs?.[0].expiry).toEqual({ selector: 'absolute', expiration_ms: Date.UTC(2026, 9, 9, 20, 0, 0) });
    expect(rowsOf(legs ?? [])[0].expiry.date).toBe('2026-10-09');
  });

  it('starts a new row as a bought call that expires as the row above it does, under an id no row has', () => {
    const rows = rowsOf(BULL_CALL_SPREAD_LEGS);

    const added = addedRow([...rows, { ...rows[0], leg_id: 'leg_3' }], OPTION_CATALOGUE);

    expect(added).toMatchObject({ leg_id: 'leg_4', position: 'long', right: 'call', qty_ratio: 1, expiry: { selector: 'nearest_weekly' } });
    expect(added?.strike).toEqual({ selector: DELTA.selector, value: null, tolerance: 0.05, from_leg: null });
  });

  it('starts a distance measured from the first other leg, and leaves it to be chosen again once that leg is removed', () => {
    const rows = rowsOf(BULL_CALL_SPREAD_LEGS);
    const third = onStrikeRule({ ...rows[0], leg_id: 'leg_3' }, DISTANCE, rows);
    expect(third.strike).toEqual({ selector: 'leg_distance', value: null, tolerance: 0, from_leg: 'long_call' });

    const left = withoutRow([...rows, { ...third, strike: { ...third.strike, value: 10, from_leg: 'short_call' } }], 'long_call');

    // The leg that was measured from the one removed points at nothing; the other keeps its own.
    expect(left.map((row) => [row.leg_id, row.strike.from_leg])).toEqual([['short_call', null], ['leg_3', 'short_call']]);
    expect(legsOf(left, RULES)).toBeNull();
  });

  it('says a leg in the catalogue’s own labels and units', () => {
    const fixed: OptionLegRules = { ...BULL_CALL_SPREAD_LEGS[0], leg_id: 'fixed', expiry: { selector: 'absolute', expiration_ms: Date.UTC(2026, 9, 9, 20, 0, 0) } };

    expect(legsInWords([...BULL_CALL_SPREAD_LEGS, fixed], OPTION_CATALOGUE)).toEqual([
      { leg_id: 'long_call', side: 'Buy', terms: '1 call · Delta target 0.5 delta · Nearest weekly' },
      { leg_id: 'short_call', side: 'Sell', terms: '1 call · Distance from another leg 5 dollars from leg 1 · Nearest weekly' },
      { leg_id: 'fixed', side: 'Buy', terms: "1 call · Delta target 0.5 delta · Fixed date Oct 9 '26" },
    ]);
  });
});

describe('the option order terms', () => {
  const OFFERED = OPTION_CATALOGUE.order_terms;
  const TERMS = startingOrderTerms(OFFERED);

  it('start from the catalogue’s own defaults', () => {
    expect(TERMS).toEqual({ gap_limit_percent: '8', entry_wait_seconds: 20, cost_limit_percent: '10', expiry_close_lead_minutes: 45 });
    expect(orderTermsSet(TERMS, OFFERED)).toBe(true);
  });

  it('are not set before the catalogue that says what a term may be has been read', () => {
    expect(orderTermsSet(TERMS, null)).toBe(false);
  });

  it.each([
    [{ gap_limit_percent: '' }],
    [{ gap_limit_percent: '0' }],
    [{ gap_limit_percent: '0.000' }],
    [{ gap_limit_percent: '100.000001' }],
    [{ gap_limit_percent: '1e3' }],
    [{ gap_limit_percent: '1.5e-6' }],
    [{ gap_limit_percent: 'eight' }],
    [{ gap_limit_percent: '.' }],
    [{ cost_limit_percent: '7.1234567' }],
    [{ cost_limit_percent: '-0.5' }],
    [{ entry_wait_seconds: null }],
    [{ entry_wait_seconds: 2.5 }],
    [{ entry_wait_seconds: 0 }],
    [{ entry_wait_seconds: 31 }],
    [{ expiry_close_lead_minutes: -1 }],
    [{ expiry_close_lead_minutes: 391 }],
  ])('are not set while one holds %o, outside the range the catalogue gives it', (change) => {
    expect(orderTermsSet({ ...TERMS, ...change }, OFFERED)).toBe(false);
  });

  it.each([
    [{ gap_limit_percent: '.3' }],
    [{ gap_limit_percent: '5.' }],
    [{ gap_limit_percent: '1e1' }],
    [{ gap_limit_percent: '1E2' }],
    [{ gap_limit_percent: '1e-6' }],
    [{ gap_limit_percent: '1.2300000' }],
    [{ gap_limit_percent: '100' }],
    [{ cost_limit_percent: '0' }],
    [{ cost_limit_percent: '0.000001' }],
    [{ entry_wait_seconds: 1 }],
    [{ entry_wait_seconds: 30 }],
    [{ expiry_close_lead_minutes: 0 }],
    [{ expiry_close_lead_minutes: 390 }],
  ])('are set while one holds %o, inside the range the catalogue gives it', (change) => {
    expect(orderTermsSet({ ...TERMS, ...change }, OFFERED)).toBe(true);
  });

  it('follow the catalogue when it gives a term another range: the form holds no bound of its own', () => {
    const wider = { ...OFFERED, entry_wait_seconds: { ...OFFERED.entry_wait_seconds, bounds: { ...OFFERED.entry_wait_seconds.bounds, max: '300' } } };

    expect(orderTermsSet({ ...TERMS, entry_wait_seconds: 120 }, OFFERED)).toBe(false);
    expect(orderTermsSet({ ...TERMS, entry_wait_seconds: 120 }, wider)).toBe(true);
  });

  it('read on one line under the catalogue’s labels and units', () => {
    expect(orderTermsInWords(TERMS, OFFERED)).toBe(
      'Widest bid-ask gap, each leg 8% of mid · Entry wait 20 seconds · Whole-structure cost 10% of cash set aside · Expiry-day close 45 min before close',
    );
  });
});

describe('the preview’s question', () => {
  const SUBJECT = {
    underlying: 'SPY', structure: 'bull_call_spread' as const, legs: BULL_CALL_SPREAD_LEGS, listensTo: 'SPY', structures: 2,
    terms: startingOrderTerms(OPTION_CATALOGUE.order_terms), offered: OPTION_CATALOGUE.order_terms,
  };

  it('is the structure on screen under the page’s key, with the owner’s size and four order terms', () => {
    expect(previewNotAsked(SUBJECT)).toBeNull();
    expect(previewRequest('preview-key-1', SUBJECT)).toEqual({
      preview_key: 'preview-key-1', underlying: 'SPY', listens_to: 'SPY', structure: 'bull_call_spread',
      legs: BULL_CALL_SPREAD_LEGS, structures: 2,
      gap_limit_percent: '8', entry_wait_seconds: 20, cost_limit_percent: '10', expiry_close_lead_minutes: 45,
    });
  });

  it.each([
    ['a half-typed underlying', { underlying: 'sp' }],
    ['a size that is no whole number', { structures: 1.5 }],
    ['a size of no structures', { structures: 0 }],
    ['order terms not read yet', { terms: null }],
    ['a catalogue not read yet', { offered: null }],
    ['a cost limit that is no percent', { terms: { ...SUBJECT.terms, cost_limit_percent: '' } }],
    ['an entry wait longer than the catalogue takes', { terms: { ...SUBJECT.terms, entry_wait_seconds: 31 } }],
  ])('is not asked with %s, and the page can say why', (_what, change) => {
    expect(previewRequest('preview-key-1', { ...SUBJECT, ...change })).toBeNull();
    expect(previewNotAsked({ ...SUBJECT, ...change })).toMatch(/^Nothing is asked yet/);
  });
});

describe('an option preview’s figures', () => {
  it('calls a quote as old as its older side, in whole seconds', () => {
    expect(quoteAgeSeconds({ bid: '1.22', bid_age_ms: 2_300, ask: '1.27', ask_age_ms: 600 })).toBe(2);
  });
});
