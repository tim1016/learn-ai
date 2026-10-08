/**
 * What the option structure routes answer, for the specs of the builder and
 * of Deploy's Options branch (#2913).
 *
 * Pure data, with no test-runner imports: `tsconfig.json` excludes only
 * `*.spec.ts` from the app compilation. The catalogue is the server's own
 * answer, cut to two structures, with stand-ins for its descriptions. A preview's sentences are stand-ins too: the
 * server writes the real ones, and no spec pins their wording.
 */

import type { components } from '../../../../api/broker.types';
import type { OptionStructureCatalogue, OptionStructurePreview } from '../../v2-panel/lib/broker-v2-panel.service';
import type { OptionLegRules } from './option-structure';

type OptionPreviewLeg = components['schemas']['OptionPreviewLeg'];
type OptionPreviewVerdict = components['schemas']['OptionPreviewVerdict'];

// 2026-10-09 16:00 America/New_York.
const OCT_9_2026_MS = Date.UTC(2026, 9, 9, 20, 0, 0);

/** A bull call spread's starting legs, as the catalogue sends them. */
export const BULL_CALL_SPREAD_LEGS: OptionLegRules[] = [
  {
    leg_id: 'long_call', position: 'long', qty_ratio: 1, right: 'call',
    strike: { selector: 'delta', target: 0.5, tolerance_delta: 0.05 },
    expiry: { selector: 'nearest_weekly' },
  },
  {
    leg_id: 'short_call', position: 'short', qty_ratio: 1, right: 'call',
    strike: { selector: 'leg_distance', leg_id: 'long_call', dollars: 5, tolerance_dollars: 0 },
    expiry: { selector: 'nearest_weekly' },
  },
];

export const LONG_CALL_LEGS: OptionLegRules[] = [BULL_CALL_SPREAD_LEGS[0]];

const NO_MS = { min_ms: null, max_ms: null } as const;

export const OPTION_CATALOGUE: OptionStructureCatalogue = {
  groups: [
    {
      label: 'One leg',
      structures: [
        {
          structure: 'long_call', label: 'Long call', in_a_sentence: 'a long call on',
          description: 'Stand-in: what a long call is.', legs: LONG_CALL_LEGS,
        },
      ],
    },
    {
      label: 'Two legs',
      structures: [
        {
          structure: 'bull_call_spread', label: 'Bull call spread', in_a_sentence: 'a bull call spread on',
          description: 'Stand-in: what a bull call spread is.', legs: BULL_CALL_SPREAD_LEGS,
        },
      ],
    },
  ],
  custom_label: 'Custom',
  custom_in_a_sentence: 'a custom option structure on',
  custom_description: 'Stand-in: what Custom starts from.',
  max_legs: 4,
  leg_quantity_bounds: {
    min: '1', min_included: true, max: '1000', max_included: true, ...NO_MS, step: '1', sentence: 'a whole number from 1 to 1,000',
  },
  structures_bounds: {
    min: '1', min_included: true, max: '100', max_included: true, ...NO_MS, step: '1', sentence: 'a whole number from 1 to 100',
  },
  strike_rules: [
    {
      selector: 'delta', label: 'Delta target',
      value: {
        field: 'target', unit: 'delta', unit_label: 'delta',
        bounds: { min: '0', min_included: false, max: '1', max_included: false, ...NO_MS, step: null, sentence: 'a number above 0 and below 1' },
      },
      measured_from: null,
      tolerance: {
        field: 'tolerance_delta', unit: 'delta', unit_label: 'delta', default: 0.05,
        bounds: { min: '0', min_included: false, max: '1', max_included: false, ...NO_MS, step: null, sentence: 'a number above 0 and below 1' },
      },
    },
    {
      selector: 'atm_offset', label: 'Steps from the money',
      value: {
        field: 'offset', unit: 'strikes', unit_label: 'strikes',
        bounds: { min: null, min_included: true, max: null, max_included: true, ...NO_MS, step: '1', sentence: 'a whole number' },
      },
      measured_from: null,
      tolerance: null,
    },
    {
      selector: 'leg_distance', label: 'Distance from another leg',
      value: {
        field: 'dollars', unit: 'dollars', unit_label: 'dollars',
        bounds: {
          min: '-99999.999', min_included: false, max: '99999.999', max_included: false, ...NO_MS, step: '0.001',
          sentence: 'a number above -99,999.999 and below 99,999.999, no finer than 0.001',
        },
      },
      measured_from: { field: 'leg_id', unit: 'leg', unit_label: 'leg' },
      tolerance: {
        field: 'tolerance_dollars', unit: 'dollars', unit_label: 'dollars', default: 0,
        bounds: {
          min: '0', min_included: true, max: '99999.999', max_included: false, ...NO_MS, step: '0.001',
          sentence: 'a number from 0 to below 99,999.999, no finer than 0.001',
        },
      },
    },
    {
      selector: 'percent', label: 'Percent from price',
      value: {
        field: 'percent', unit: 'percent', unit_label: 'percent',
        bounds: { min: '-100', min_included: false, max: '1000', max_included: true, ...NO_MS, step: null, sentence: 'a number above -100 and up to 1,000' },
      },
      measured_from: null,
      tolerance: {
        field: 'tolerance_percent', unit: 'percent', unit_label: 'percent', default: 0.5,
        bounds: { min: '0', min_included: false, max: '100', max_included: true, ...NO_MS, step: null, sentence: 'a number above 0 and up to 100' },
      },
    },
  ],
  expiry_rules: [
    {
      selector: 'min_dte', label: 'At least N days out',
      value: {
        field: 'days', unit: 'days', unit_label: 'days',
        bounds: { min: '1', min_included: true, max: null, max_included: true, ...NO_MS, step: '1', sentence: 'a whole number of 1 or more' },
      },
    },
    { selector: 'nearest_weekly', label: 'Nearest weekly', value: null },
    { selector: 'same_day', label: 'Same day', value: null },
    {
      selector: 'absolute', label: 'Fixed date',
      value: {
        field: 'expiration_ms', unit: 'date', unit_label: 'date',
        bounds: {
          min: null, min_included: true, max: null, max_included: true, min_ms: 946760400000, max_ms: 4102434000000, step: null,
          sentence: 'a date from Jan 1, 2000 to Dec 31, 2099',
        },
      },
    },
  ],
  order_terms: {
    gap_limit_percent: {
      label: 'Widest bid-ask gap, each leg', unit_label: '% of mid', default: '8',
      bounds: {
        min: '0', min_included: false, max: '100', max_included: true, ...NO_MS, step: '0.000001',
        sentence: 'a number above 0 and up to 100, no finer than 0.000001',
      },
      help: 'An entry is skipped when any leg\'s bid and ask are further apart than this, as a percent of the price halfway between them.',
      range_reason: null,
    },
    entry_wait_seconds: {
      label: 'Entry wait', unit_label: 'seconds', default: 20,
      bounds: { min: '1', min_included: true, max: '30', max_included: true, ...NO_MS, step: '1', sentence: 'a whole number from 1 to 30' },
      help: 'How long an entry order waits for its fill. What has not filled by then is cancelled.',
      range_reason: 'The bot has 50 seconds from its decision bar\'s close to learn what an entry came to.',
    },
    cost_limit_percent: {
      label: 'Whole-structure cost', unit_label: '% of cash set aside', default: '10',
      bounds: {
        min: '0', min_included: true, max: '100', max_included: true, ...NO_MS, step: '0.000001',
        sentence: 'a number from 0 to 100, no finer than 0.000001',
      },
      help: 'An entry is skipped when opening the whole structure and closing it at once would cost more than this percent of what the entry sets aside.',
      range_reason: null,
    },
    expiry_close_lead_minutes: {
      label: 'Expiry-day close', unit_label: 'min before close', default: 45,
      bounds: { min: '0', min_included: true, max: '390', max_included: true, ...NO_MS, step: '1', sentence: 'a whole number from 0 to 390' },
      help: 'On the day a held contract expires, the position is closed this long before the session closes.',
      range_reason: null,
    },
  },
  order_term_notes: {
    entries_sentence: 'Entries are limit orders at the quoted price. An entry that has not filled when its wait is over is cancelled.',
    exits_sentence: 'Exits are market orders. A market order has no price limit.',
    hours_sentence: 'Regular hours only.',
    alpaca_sentence: 'Alpaca\'s documentation says that when an account cannot afford to exercise an expiring position, Alpaca will sell the position itself within 1 hour before expiry.',
  },
};

const GREEN: OptionPreviewVerdict = {
  can_be_deployed: true,
  deploy_sentence: 'Stand-in: it can be deployed.',
  deploy_reason_code: null,
  resolves_as_built: true,
  resolves_sentence: 'Stand-in: it resolves to what was built.',
  resolves_reason_code: null,
  leg_ids: [],
  red: false,
};

/** A verdict the server marks red: the structure cannot be used. */
export const RED_VERDICT: OptionPreviewVerdict = {
  ...GREEN,
  resolves_as_built: false,
  resolves_sentence: 'Stand-in: leg 2 lands on the strike of leg 1.',
  resolves_reason_code: 'OPTION_STRUCTURE_STAND_IN',
  leg_ids: ['short_call'],
  red: true,
};

const PICKED: OptionPreviewLeg[] = [
  {
    leg_id: 'long_call', number: 1, position: 'long', ratio: 1,
    contract: { occ_symbol: 'SPY261009C00585000', underlying: 'SPY', expiry_ms: OCT_9_2026_MS, right: 'call', strike: '585' },
    quote: { bid: '3.10', bid_age_ms: 900, ask: '3.16', ask_age_ms: 1_400 },
    gap_percent_of_mid: '1.92', over_gap_limit: false, gap_sentence: 'Stand-in: its gap is inside the limit.',
    delta: 0.4812, implied_volatility: 0.18, delta_status: 'solved', delta_status_sentence: 'Stand-in: worked out from its mid.',
    ibkr_delta: 0.4701,
  },
  {
    leg_id: 'short_call', number: 2, position: 'short', ratio: 1,
    contract: { occ_symbol: 'SPY261009C00590000', underlying: 'SPY', expiry_ms: OCT_9_2026_MS, right: 'call', strike: '590' },
    quote: { bid: '1.22', bid_age_ms: 2_300, ask: '1.27', ask_age_ms: 600 },
    gap_percent_of_mid: '4.02', over_gap_limit: false, gap_sentence: 'Stand-in: its gap is inside the limit.',
    delta: 0.3149, implied_volatility: 0.17, delta_status: 'solved', delta_status_sentence: 'Stand-in: worked out from its mid.',
    ibkr_delta: 0.3098,
  },
];

/** An answer with no figures: a state's sentence and nothing else. */
export function previewWithoutFigures(
  state: OptionStructurePreview['state'],
  notice: string,
  over: Partial<OptionStructurePreview> = {},
): OptionStructurePreview {
  return {
    state,
    reason_code: 'OPTION_PREVIEW_STAND_IN',
    notice,
    fault: null,
    leg_ids: [],
    contract: null,
    ask_again_in_ms: null,
    underlying: 'SPY',
    structure: 'bull_call_spread',
    structure_label: 'Bull call spread',
    structure_in_a_sentence: 'a bull call spread on',
    structures: 1,
    gap_limit_percent: '8',
    cost_limit_percent: '10',
    order_terms: null,
    legs_asked: [],
    quotes_read_ms: Date.UTC(2026, 9, 6, 13, 38, 7),
    feed_generation: null,
    oldest_quote_age_ms: null,
    quotes_read_sentence: null,
    figures_good_until_ms: null,
    figures_good_for_ms: null,
    figures_stale_sentence: null,
    underlying_quote: null,
    the_money: null,
    the_money_sentence: null,
    verdict: { ...GREEN, resolves_as_built: null, resolves_sentence: 'Stand-in: what it resolves to is not known.' },
    legs: [],
    money: null,
    at_expiry: null,
    lean: null,
    lean_unknown_sentence: null,
    decay: null,
    entry_refusals: [],
    entry_sentence: null,
    watched: null,
    pricing_inputs: null,
    preview_sentence: 'Stand-in: the bot picks again at every entry.',
    ...over,
  };
}

/**
 * A resolved answer for a structure that pays at entry: the server signs the
 * net price and the premium as negative, and says "received".
 */
export function previewThatPays(structures = 1): OptionStructurePreview {
  const answer = resolvedPreview();
  return {
    ...answer,
    structures,
    money: answer.money && {
      ...answer.money, structures, net_price: '-1.20', premium_is: 'received', premium_is_label: 'received',
      net_entry_premium_cents: -12_000 * structures, net_entry_premium_per_structure_cents: -12_000,
      net_price_usd: '$1.20', premium_usd: structures === 1 ? '$120.00' : '$240.00', premium_per_structure_usd: '$120.00',
      premium_sentence: 'Stand-in: premium received.',
    },
  };
}

/** A resolved answer: a bull call spread on SPY, picked and priced. */
export function resolvedPreview(over: Partial<OptionStructurePreview> = {}): OptionStructurePreview {
  return {
    ...previewWithoutFigures('resolved', ''),
    reason_code: null,
    notice: null,
    feed_generation: 7,
    oldest_quote_age_ms: 2_300,
    quotes_read_sentence: 'Stand-in: IBKR live quotes, and how old the oldest was.',
    figures_good_until_ms: Date.UTC(2026, 9, 6, 13, 39, 7),
    figures_good_for_ms: 60_000,
    figures_stale_sentence: 'Stand-in: these figures are from quotes read more than a minute ago.',
    entry_sentence: 'Stand-in: nothing in these picks would stop an entry right now.',
    underlying_quote: { bid: '585.10', bid_age_ms: 300, ask: '585.14', ask_age_ms: 300 },
    the_money: '585.12',
    the_money_sentence: 'Stand-in: strikes are counted from the live mid.',
    verdict: GREEN,
    legs: PICKED,
    money: {
      structures: 1, multiplier: 100, net_price: '1.94', premium_is: 'paid',
      net_entry_premium_cents: 19_400, net_entry_premium_per_structure_cents: 19_400,
      broker_requirement_cents: 0, fee_estimate_cents: 37, entry_commitment_cents: 19_437,
      round_trip_cost_cents: 1_100, round_trip_cost_percent: '5.66', cost_limit_percent: '10', over_cost_limit: false,
      cost_sentence: 'Stand-in: what opening and closing at once would cost.',
      fees_sentence: 'Stand-in: fees are an estimate.',
      net_price_usd: '$1.94', premium_usd: '$194.00', premium_per_structure_usd: '$194.00', premium_is_label: 'paid',
      premium_sentence: 'Stand-in: premium paid.',
      broker_requirement_usd: '$0.00', fee_estimate_usd: '$0.37', entry_commitment_usd: '$194.37', round_trip_cost_usd: '$11.00',
    },
    at_expiry: {
      estimated: true, loss_cents: 19_400, loss_holds: [{ from_price: '0', to_price: '585' }],
      gain_cents: 30_600, gain_has_no_limit: false, gain_holds: [{ from_price: '590', to_price: null }],
      break_even_prices: ['586.94'],
      loss_sentence: 'Stand-in: the most it loses at expiry.',
      gain_sentence: 'Stand-in: the most it gains at expiry.',
      break_even_sentence: 'Stand-in: where it is even.',
      assumptions_sentence: 'Stand-in: at expiry, if held, before fees.',
      warning_sentence: 'Stand-in: a market exit can do worse.',
      loss_usd: '$194.00', gain_usd: '$306.00',
      sketch: [
        { price: '583', result_cents: -19_400, result_usd: '-$194.00' },
        { price: '592', result_cents: 30_600, result_usd: '$306.00' },
      ],
    },
    lean: {
      net_delta: 0.17, shares_per_structure: 17, shares: 17, direction: 'up', about_flat_below_shares_per_structure: 5,
      sentence: 'Stand-in: which way it leans.', gains_when_underlying_falls: false, warning_sentence: null,
    },
    watched: {
      ranges: [{
        right: 'call', expiry_ms: OCT_9_2026_MS, lowest_strike: '560', highest_strike: '610', contracts: 21,
        sentence: 'Stand-in: calls expiring Oct 9 \'26, strikes 560 to 610.',
      }],
      sentence: 'Stand-in: every contract these rules can pick from holds a quote line.',
      lines_wanted: 21, lines_held: 21, lines_refused: 0, line_budget: 100, lines_open: 22,
    },
    ...over,
  };
}
