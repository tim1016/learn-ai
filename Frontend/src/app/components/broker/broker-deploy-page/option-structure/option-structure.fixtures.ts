/**
 * What the option structure routes answer, for the specs of the builder and
 * of Deploy's Options branch (#2913).
 *
 * Pure data, with no test-runner imports: `tsconfig.json` excludes only
 * `*.spec.ts` from the app compilation. Every sentence here is a stand-in.
 * The server writes the real ones, and no spec pins their wording.
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

export const OPTION_CATALOGUE: OptionStructureCatalogue = {
  groups: [
    {
      label: 'One leg',
      structures: [
        { structure: 'long_call', label: 'Long call', description: 'Stand-in: what a long call is.', legs: LONG_CALL_LEGS },
      ],
    },
    {
      label: 'Two legs',
      structures: [
        {
          structure: 'bull_call_spread', label: 'Bull call spread',
          description: 'Stand-in: what a bull call spread is.', legs: BULL_CALL_SPREAD_LEGS,
        },
      ],
    },
  ],
  custom_label: 'Custom',
  custom_description: 'Stand-in: what Custom starts from.',
  max_legs: 4,
  strike_rules: [
    {
      selector: 'delta', label: 'Delta target',
      value: { field: 'target', unit: 'delta', unit_label: 'delta' },
      tolerance: { field: 'tolerance_delta', unit: 'delta', unit_label: 'delta', default: 0.05 },
    },
    {
      selector: 'atm_offset', label: 'Steps from the money',
      value: { field: 'offset', unit: 'strikes', unit_label: 'strikes' },
      tolerance: null,
    },
    {
      selector: 'leg_distance', label: 'Distance from another leg',
      value: { field: 'dollars', unit: 'dollars', unit_label: 'dollars' },
      tolerance: { field: 'tolerance_dollars', unit: 'dollars', unit_label: 'dollars', default: 0 },
    },
    {
      selector: 'percent', label: 'Percent from price',
      value: { field: 'percent', unit: 'percent', unit_label: 'percent' },
      tolerance: { field: 'tolerance_percent', unit: 'percent', unit_label: 'percent', default: 0.5 },
    },
  ],
  expiry_rules: [
    { selector: 'min_dte', label: 'At least N days out', value: { field: 'days', unit: 'days', unit_label: 'days' } },
    { selector: 'nearest_weekly', label: 'Nearest weekly', value: null },
    { selector: 'same_day', label: 'Same day', value: null },
    { selector: 'absolute', label: 'Fixed date', value: { field: 'expiration_ms', unit: 'date', unit_label: 'date' } },
  ],
  order_terms: {
    gap_limit_percent: { label: 'Widest bid-ask gap, each leg', unit_label: '% of mid', default: '8' },
    entry_wait_seconds: { label: 'Entry wait', unit_label: 'seconds', default: 30 },
    cost_limit_percent: { label: 'Whole-structure cost', unit_label: '% of cash set aside', default: '10' },
    expiry_close_lead_minutes: { label: 'Expiry-day close', unit_label: 'min before close', default: 45 },
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
    leg_id: 'long_call', position: 'long', ratio: 1,
    contract: { occ_symbol: 'SPY261009C00585000', underlying: 'SPY', expiry_ms: OCT_9_2026_MS, right: 'call', strike: '585' },
    quote: { bid: '3.10', bid_age_ms: 900, ask: '3.16', ask_age_ms: 1_400 },
    gap_percent_of_mid: '1.92', delta: 0.4812, implied_volatility: 0.18, delta_status: 'solved', ibkr_delta: 0.4701,
  },
  {
    leg_id: 'short_call', position: 'short', ratio: 1,
    contract: { occ_symbol: 'SPY261009C00590000', underlying: 'SPY', expiry_ms: OCT_9_2026_MS, right: 'call', strike: '590' },
    quote: { bid: '1.22', bid_age_ms: 2_300, ask: '1.27', ask_age_ms: 600 },
    gap_percent_of_mid: '4.02', delta: 0.3149, implied_volatility: 0.17, delta_status: 'solved', ibkr_delta: 0.3098,
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
    structures: 1,
    gap_limit_percent: '8',
    cost_limit_percent: '10',
    quotes_read_ms: Date.UTC(2026, 9, 6, 13, 38, 7),
    feed_generation: null,
    oldest_quote_age_ms: null,
    underlying_quote: null,
    the_money: null,
    the_money_sentence: null,
    verdict: { ...GREEN, resolves_as_built: null, resolves_sentence: 'Stand-in: what it resolves to is not known.' },
    legs: [],
    money: null,
    at_expiry: null,
    lean: null,
    lean_unknown_sentence: null,
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
      ...answer.money, structures, net_price: '-1.20', premium_is: 'received',
      net_entry_premium_cents: -12_000 * structures, net_entry_premium_per_structure_cents: -12_000,
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
    },
    lean: {
      net_delta: 0.17, shares_per_structure: 17, shares: 17, direction: 'up', about_flat_below_shares_per_structure: 5,
      sentence: 'Stand-in: which way it leans.', gains_when_underlying_falls: false, warning_sentence: null,
    },
    watched: {
      ranges: [{ right: 'call', expiry_ms: OCT_9_2026_MS, lowest_strike: '560', highest_strike: '610', contracts: 21 }],
      lines_wanted: 21, lines_held: 21, lines_refused: 0, line_budget: 100, lines_open: 22,
    },
    ...over,
  };
}
