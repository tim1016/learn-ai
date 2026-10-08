import type { components } from '../../../../api/broker.types';
import { optionExpiryAnchorMs, optionExpiryDate } from '../../../../shared/option-contract/option-expiry-date';
import { formatReceiptLabel } from '../../../../shared/pipes/receipt-label.pipe';
import { formatTimestampDisplay } from '../../../../shared/timestamp/timestamp-display';
import type {
  OptionStructureCatalogue,
  OptionStructurePreviewRequest,
} from '../../v2-panel/lib/broker-v2-panel.service';
import { canonicalJson } from '../deploy-draft.store';

export type NamedStructure = components['schemas']['NamedStructure'];
export type NamedStructureStart = components['schemas']['NamedStructureStart'];
export type OptionLegRules = components['schemas']['OptionLegRules'];
export type OptionStrikeRule = components['schemas']['OptionStrikeRule'];
export type OptionExpiryRule = components['schemas']['OptionExpiryRule'];
export type OptionOrderTermDefaults = components['schemas']['OptionOrderTermDefaults'];
export type OptionValueBounds = components['schemas']['OptionValueBounds'];
type StrikeSelector = OptionLegRules['strike'];
type ExpirySelector = OptionLegRules['expiry'];
/** The rules a leg may be given, as the catalogue lists them. */
export type OptionRules = Pick<OptionStructureCatalogue, 'strike_rules' | 'expiry_rules'>;

// ── What the Deploy form holds for an option bot ────────────────────────────

/**
 * The option order terms as the owner set them, under the names the
 * catalogue's `order_terms` gives them. A percent is kept as typed, so an
 * exact decimal never passes through a float. A whole-number term is `null`
 * while its box is empty. Whether a term can be sent as it stands is
 * {@link orderTermsSet}'s to say.
 */
export interface OptionOrderTerms {
  readonly gap_limit_percent: string;
  readonly entry_wait_seconds: number | null;
  readonly cost_limit_percent: string;
  readonly expiry_close_lead_minutes: number | null;
}

/**
 * What an option bot trades (#2913), as the Deploy form holds it: the
 * underlying, the structure's name and its legs under the preview request's
 * own field names and in its own shapes, and the order terms.
 */
export interface DeployOptionSettings {
  /** "Options on": the one underlying every leg is on. */
  readonly underlying: string;
  /** The named structure the legs still are; `null` is Custom. */
  readonly structure: NamedStructure | null;
  readonly legs: readonly OptionLegRules[];
  /** `null` until the catalogue's starting terms have been read. */
  readonly order_terms: OptionOrderTerms | null;
}

/** Order terms every one of which can be sent: each whole-number term holds its number. */
export interface SendableOrderTerms extends OptionOrderTerms {
  readonly entry_wait_seconds: number;
  readonly expiry_close_lead_minutes: number;
}

/** Options on `underlying`, before the owner has built anything. */
export function freshOptionSettings(underlying: string): DeployOptionSettings {
  return { underlying, structure: null, legs: [], order_terms: null };
}

// ── A leg as a row of the builder ───────────────────────────────────────────

/** A leg's strike rule as its row holds it. */
export interface StrikeRow {
  readonly selector: StrikeSelector['selector'];
  /** The rule's own number: a delta, a count of strikes, dollars or a percent. */
  readonly value: number | null;
  /** The builder's "Within". Unused by a rule that has none. */
  readonly tolerance: number | null;
  /** The leg a distance is measured from. */
  readonly from_leg: string | null;
}

/** A leg's expiry rule as its row holds it. */
export interface ExpiryRow {
  readonly selector: ExpirySelector['selector'];
  readonly days: number | null;
  /** `YYYY-MM-DD`; empty while the date field holds no usable date. */
  readonly date: string;
}

/** One leg as a row of the builder holds it: a box may be empty while the owner types. */
export interface LegRow {
  readonly leg_id: string;
  readonly position: OptionLegRules['position'];
  readonly right: OptionLegRules['right'];
  readonly qty_ratio: number | null;
  readonly strike: StrikeRow;
  readonly expiry: ExpiryRow;
}

function strikeRow(strike: StrikeSelector, rules: readonly OptionStrikeRule[]): StrikeRow {
  // A leg that leaves its tolerance out is held to the rule's own, so the row shows the one in force.
  const starts = rules.find((rule) => rule.selector === strike.selector)?.tolerance?.default ?? null;
  switch (strike.selector) {
    case 'delta':
      return { selector: 'delta', value: strike.target, tolerance: strike.tolerance_delta ?? starts, from_leg: null };
    case 'atm_offset':
      return { selector: 'atm_offset', value: strike.offset, tolerance: null, from_leg: null };
    case 'leg_distance':
      return {
        selector: 'leg_distance', value: strike.dollars, tolerance: strike.tolerance_dollars ?? starts, from_leg: strike.leg_id,
      };
    case 'percent':
      return { selector: 'percent', value: strike.percent, tolerance: strike.tolerance_percent ?? starts, from_leg: null };
    case 'absolute':
      return { selector: 'absolute', value: strike.strike, tolerance: null, from_leg: null };
    case 'atm':
      return { selector: 'atm', value: null, tolerance: null, from_leg: null };
  }
}

function strikeOf(row: StrikeRow): StrikeSelector | null {
  const { value, tolerance } = row;
  if (row.selector === 'atm') return { selector: 'atm' };
  if (value === null) return null;
  switch (row.selector) {
    case 'delta':
      return { selector: 'delta', target: value, ...(tolerance === null ? {} : { tolerance_delta: tolerance }) };
    case 'atm_offset':
      return { selector: 'atm_offset', offset: value };
    case 'leg_distance':
      return row.from_leg === null
        ? null
        : { selector: 'leg_distance', leg_id: row.from_leg, dollars: value, ...(tolerance === null ? {} : { tolerance_dollars: tolerance }) };
    case 'percent':
      return { selector: 'percent', percent: value, ...(tolerance === null ? {} : { tolerance_percent: tolerance }) };
    case 'absolute':
      return { selector: 'absolute', strike: value };
  }
}

function expiryRow(expiry: ExpirySelector): ExpiryRow {
  switch (expiry.selector) {
    case 'min_dte':
      return { selector: 'min_dte', days: expiry.days, date: '' };
    case 'absolute':
      return { selector: 'absolute', days: null, date: optionExpiryDate(expiry.expiration_ms) };
    case 'nearest_weekly':
    case 'same_day':
      return { selector: expiry.selector, days: null, date: '' };
  }
}

function expiryOf(row: ExpiryRow): ExpirySelector | null {
  switch (row.selector) {
    case 'min_dte':
      return row.days === null ? null : { selector: 'min_dte', days: row.days };
    case 'absolute':
      return row.date === '' ? null : { selector: 'absolute', expiration_ms: optionExpiryAnchorMs(row.date) };
    case 'nearest_weekly':
      return { selector: 'nearest_weekly' };
    case 'same_day':
      return { selector: 'same_day' };
  }
}

/** A whole leg as the row the builder edits. */
export function legRow(leg: OptionLegRules, rules: readonly OptionStrikeRule[]): LegRow {
  return {
    leg_id: leg.leg_id,
    position: leg.position,
    right: leg.right,
    qty_ratio: leg.qty_ratio,
    strike: strikeRow(leg.strike, rules),
    expiry: expiryRow(leg.expiry),
  };
}

function isWholeCount(value: number | null): value is number {
  return value !== null && Number.isInteger(value) && value >= 1;
}

/** A rule's number box as the catalogue describes it: its unit, and the server's word for that unit. */
type RuleBox = Pick<OptionStrikeRule['value'], 'unit' | 'unit_label'>;

/**
 * What a rule's number box still needs, under the catalogue's label for the
 * rule and in its word for the unit, or nothing when the box holds its
 * number. A unit the server counts in whole steps takes a whole number.
 */
function numberNeeded(value: number | null, label: string, box: RuleBox | null | undefined): string | undefined {
  const whole = box?.unit === 'strikes' || box?.unit === 'days';
  if (value !== null && (!whole || Number.isInteger(value))) return undefined;
  if (!box) return `${label} needs a number.`;
  return whole ? `${label} needs a whole number of ${box.unit_label}.` : `${label} needs a number, in ${box.unit_label}.`;
}

/**
 * What a row's boxes still need before the row is a leg, each in the
 * screen's own words: form chrome, never one of the server's sentences. A
 * box that needs nothing has no entry.
 */
export interface RowProblems {
  readonly quantity?: string;
  /** The strike rule's own number. */
  readonly strike?: string;
  /** The leg a distance is measured from. */
  readonly from?: string;
  /** The builder's "Within". */
  readonly within?: string;
  /** The expiry rule's days or date. */
  readonly expiry?: string;
}

/**
 * What `row` still needs before it is a leg: a box that is empty, a count
 * that is no whole number, or a distance measured from a leg that is not
 * there. What a finished leg's numbers may be is the server's to judge.
 */
export function rowProblems(row: LegRow, rows: readonly LegRow[], rules: OptionRules): RowProblems {
  const { strike, expiry } = row;
  const strikeRule = rules.strike_rules.find((offered) => offered.selector === strike.selector);
  const expiryRule = rules.expiry_rules.find((offered) => offered.selector === expiry.selector);
  const strikeLabel = strikeRule?.label ?? formatReceiptLabel(strike.selector);
  const expiryLabel = expiryRule?.label ?? formatReceiptLabel(expiry.selector);
  const measuredFromALeg = rows.some((other) => other.leg_id === strike.from_leg && other.leg_id !== row.leg_id);
  const problems: { -readonly [Box in keyof RowProblems]: RowProblems[Box] } = {};
  if (!isWholeCount(row.qty_ratio)) problems.quantity = 'Quantity needs a whole number of 1 or more.';
  if (strike.selector !== 'atm') problems.strike = numberNeeded(strike.value, strikeLabel, strikeRule?.value);
  if (measuredFromAnotherLeg(strike, strikeRule) && !measuredFromALeg) problems.from = `${strikeLabel} needs the leg it is measured from.`;
  if (strikeRule?.tolerance) problems.within = numberNeeded(strike.tolerance, 'Within', strikeRule.tolerance);
  if (expiry.selector === 'min_dte') problems.expiry = numberNeeded(expiry.days, expiryLabel, expiryRule?.value);
  if (expiry.selector === 'absolute' && expiry.date === '') problems.expiry = `${expiryLabel} needs a date.`;
  return problems;
}

/**
 * Whether a strike is measured from another leg. The catalogue says so of
 * each rule it offers (`measured_from`); a rule it does not list is read by
 * its own selector.
 */
function measuredFromAnotherLeg(strike: Pick<StrikeRow, 'selector'>, rule: OptionStrikeRule | undefined): boolean {
  return rule === undefined ? strike.selector === 'leg_distance' : rule.measured_from !== null;
}

/** The boxes of a row, in the order they stand in it. */
const ROW_BOXES = ['quantity', 'strike', 'from', 'within', 'expiry'] as const;
export type RowBox = (typeof ROW_BOXES)[number];

/** One box's problem, to read under the row and to tie to that box alone. */
export interface RowProblemLine {
  readonly box: RowBox;
  readonly text: string;
}

/** The problems of a row as lines to read, in the order of its boxes. */
export function problemLines(problems: RowProblems): RowProblemLine[] {
  return ROW_BOXES.flatMap((box) => {
    const text = problems[box];
    return text === undefined ? [] : [{ box, text }];
  });
}

/**
 * Every row as a leg, or `null` while a row is unfinished: one that
 * {@link rowProblems} still finds something to say about.
 */
export function legsOf(rows: readonly LegRow[], rules: OptionRules): OptionLegRules[] | null {
  const legs: OptionLegRules[] = [];
  for (const row of rows) {
    const strike = strikeOf(row.strike);
    const expiry = expiryOf(row.expiry);
    if (problemLines(rowProblems(row, rows, rules)).length > 0) return null;
    // A row with nothing left to say holds all three; the checks are for the compiler.
    if (!isWholeCount(row.qty_ratio) || strike === null || expiry === null) return null;
    legs.push({ leg_id: row.leg_id, position: row.position, right: row.right, qty_ratio: row.qty_ratio, strike, expiry });
  }
  return legs;
}

/** Legs spelled one way, whatever they left to a rule's own tolerance. */
function spelled(legs: readonly OptionLegRules[], rules: OptionRules): string {
  return canonicalJson(legsOf(legs.map((leg) => legRow(leg, rules.strike_rules)), rules));
}

/**
 * The named structure these legs still are: the one whose starting legs in
 * the catalogue they match exactly. Anything else is Custom (`null`).
 */
export function structureNamed(legs: readonly OptionLegRules[], catalogue: OptionStructureCatalogue): NamedStructure | null {
  if (legs.length === 0) return null;
  const these = spelled(legs, catalogue);
  for (const group of catalogue.groups) {
    for (const start of group.structures) {
      if (spelled(start.legs, catalogue) === these) return start.structure;
    }
  }
  return null;
}

/** The catalogue's entry for a named structure. */
export function structureStart(structure: NamedStructure | null, catalogue: OptionStructureCatalogue): NamedStructureStart | null {
  if (structure === null) return null;
  return catalogue.groups.flatMap((group) => group.structures).find((start) => start.structure === structure) ?? null;
}

/**
 * A new row under `rows`: a bought call on the first strike rule the
 * catalogue lists, its number still to be typed, expiring as the row above
 * it does. `null` when the catalogue lists no rule to start one from.
 */
export function addedRow(rows: readonly LegRow[], catalogue: OptionStructureCatalogue): LegRow | null {
  const strikeRule = catalogue.strike_rules.at(0);
  const expiryRule = catalogue.expiry_rules.at(0);
  if (strikeRule === undefined || expiryRule === undefined) return null;
  let number = rows.length + 1;
  while (rows.some((row) => row.leg_id === `leg_${number}`)) number += 1;
  const legId = `leg_${number}`;
  return {
    leg_id: legId,
    position: 'long',
    right: 'call',
    qty_ratio: 1,
    strike: strikeOnRule(strikeRule, legId, rows),
    expiry: rows.at(-1)?.expiry ?? { selector: expiryRule.selector, days: null, date: '' },
  };
}

/** A strike rule newly chosen for a leg: its number empty, its "Within" at the rule's own start. */
function strikeOnRule(rule: OptionStrikeRule, legId: string, rows: readonly LegRow[]): StrikeRow {
  const from = rule.measured_from !== null ? rows.find((other) => other.leg_id !== legId)?.leg_id ?? null : null;
  return { selector: rule.selector, value: null, tolerance: rule.tolerance?.default ?? null, from_leg: from };
}

/** `row` on another strike rule. A distance starts measured from the first other leg. */
export function onStrikeRule(row: LegRow, rule: OptionStrikeRule, rows: readonly LegRow[]): LegRow {
  return { ...row, strike: strikeOnRule(rule, row.leg_id, rows) };
}

/**
 * `rows` without one leg. A distance that was measured from it is left
 * measured from no leg, for the owner to choose again: the row is unfinished
 * until they do, and never points at a leg that is gone.
 */
export function withoutRow(rows: readonly LegRow[], legId: string): LegRow[] {
  return rows
    .filter((row) => row.leg_id !== legId)
    .map((row) => (row.strike.from_leg === legId ? { ...row, strike: { ...row.strike, from_leg: null } } : row));
}

/** `row` on another expiry rule, with nothing typed for it yet. */
export function onExpiryRule(row: LegRow, rule: OptionExpiryRule): LegRow {
  return { ...row, expiry: { selector: rule.selector, days: null, date: '' } };
}

// ── Words ───────────────────────────────────────────────────────────────────

const SIDE_IN_WORDS: Record<OptionLegRules['position'], string> = { long: 'Buy', short: 'Sell' };

/** A leg's side as a word. Colour is never the only sign of it. */
export function sideInWords(position: OptionLegRules['position']): string {
  return SIDE_IN_WORDS[position];
}

/** A number with its unit as the server words the unit: `8% of mid`, `30 seconds`. */
function withUnit(value: number | string, unitLabel: string): string {
  return unitLabel.startsWith('%') ? `${value}${unitLabel}` : `${value} ${unitLabel}`;
}

/** One leg in words, for a structure with no description of its own. */
export interface LegInWords {
  readonly leg_id: string;
  readonly side: string;
  /** What is bought or sold and by which rules, in the catalogue's own labels. */
  readonly terms: string;
}

/** Each leg in words, built from the catalogue's labels for its rules. */
export function legsInWords(legs: readonly OptionLegRules[], catalogue: OptionStructureCatalogue): LegInWords[] {
  return legs.map((leg) => {
    const { strike, expiry } = legRow(leg, catalogue.strike_rules);
    const strikeRule = catalogue.strike_rules.find((rule) => rule.selector === strike.selector);
    const expiryRule = catalogue.expiry_rules.find((rule) => rule.selector === expiry.selector);
    const measuredFrom = legs.findIndex((other) => other.leg_id === strike.from_leg) + 1;
    const strikeWords = [
      strikeRule?.label ?? formatReceiptLabel(strike.selector),
      ...(strike.value === null ? [] : [strikeRule ? withUnit(strike.value, strikeRule.value.unit_label) : String(strike.value)]),
      ...(measuredFrom > 0 ? [`from leg ${measuredFrom}`] : []),
    ].join(' ');
    const expiryWords = [
      expiryRule?.label ?? formatReceiptLabel(expiry.selector),
      ...(expiry.days === null ? [] : [expiryRule?.value ? withUnit(expiry.days, expiryRule.value.unit_label) : String(expiry.days)]),
      ...(leg.expiry.selector === 'absolute'
        ? [formatTimestampDisplay(leg.expiry.expiration_ms, { mode: 'date-et', granularity: 'short-date' })]
        : []),
    ].join(' ');
    return {
      leg_id: leg.leg_id,
      side: sideInWords(leg.position),
      terms: [`${leg.qty_ratio} ${leg.right}`, strikeWords, expiryWords].join(' · '),
    };
  });
}

// ── Order terms ─────────────────────────────────────────────────────────────

/** The order terms a Deploy starts from: the catalogue's own defaults. */
export function startingOrderTerms(defaults: OptionOrderTermDefaults): OptionOrderTerms {
  return {
    gap_limit_percent: defaults.gap_limit_percent.default,
    entry_wait_seconds: defaults.entry_wait_seconds.default,
    cost_limit_percent: defaults.cost_limit_percent.default,
    expiry_close_lead_minutes: defaults.expiry_close_lead_minutes.default,
  };
}

/** The order terms that are a percent, and those that are a whole number. */
export type OptionPercentTerm = 'gap_limit_percent' | 'cost_limit_percent';
export type OptionCountTerm = 'entry_wait_seconds' | 'expiry_close_lead_minutes';
/** The four terms, in the order the How step lays them out. */
export const ORDER_TERMS = ['gap_limit_percent', 'entry_wait_seconds', 'cost_limit_percent', 'expiry_close_lead_minutes'] as const;

/** A decimal as the routes read one written: a sign, digits, a fraction, a power of ten. `.3` and `1e1` are both read. */
const WRITTEN_DECIMAL = /^\s*(?=-?\.?\d)-?(\d*)\.?(\d*)(?:e([+-]?\d+))?\s*$/i;

/** The decimal places a written number has once its trailing zeros are dropped, as the routes count them. */
function decimalPlaces(whole: string, fraction: string, exponent: number): number {
  const digits = whole + fraction;
  const significant = digits.replace(/0+$/, '');
  return significant === '' ? 0 : Math.max(0, fraction.length - (digits.length - significant.length) - exponent);
}

/**
 * Whether a number as written is one the server takes for a box: inside the
 * range the catalogue gives the box, ends counted as it says, and no finer
 * than its step. A step is read as a count of decimal places (`1` is a whole
 * number, `0.000001` six places), which is every step the catalogue names.
 * The text is only read here: what is sent is what the owner typed.
 */
function insideBounds(written: string, bounds: OptionValueBounds): boolean {
  const parts = WRITTEN_DECIMAL.exec(written);
  const value = Number(written);
  if (parts === null || !Number.isFinite(value)) return false;
  const places = bounds.step === null ? null : (bounds.step.split('.')[1] ?? '').length;
  if (places !== null && decimalPlaces(parts[1], parts[2], Number(parts[3] ?? 0)) > places) return false;
  if (bounds.min !== null && (bounds.min_included ? value < Number(bounds.min) : value <= Number(bounds.min))) return false;
  return bounds.max === null || (bounds.max_included ? value <= Number(bounds.max) : value < Number(bounds.max));
}

/**
 * Why an order term as it stands cannot be sent, or `null` when it can. The
 * range is the catalogue's (#2913), and so are the words for it: the form
 * holds no bound of its own.
 */
export function termProblem(value: string | number | null, bounds: OptionValueBounds): string | null {
  return insideBounds(value === null ? '' : String(value), bounds) ? null : `Enter ${bounds.sentence}.`;
}

/**
 * Whether every order term holds something the routes take, by the ranges
 * the catalogue gives them. Not before the catalogue has been read: until
 * then nothing says what a term may be.
 */
export function orderTermsSet(terms: OptionOrderTerms | null, offered: OptionOrderTermDefaults | null): terms is SendableOrderTerms {
  return terms !== null && offered !== null && ORDER_TERMS.every((name) => termProblem(terms[name], offered[name].bounds) === null);
}

/** The order terms on one line, each under the catalogue's own label and unit. */
export function orderTermsInWords(terms: OptionOrderTerms, defaults: OptionOrderTermDefaults): string {
  const term = (name: keyof OptionOrderTerms): string =>
    `${defaults[name].label} ${withUnit(terms[name] ?? '', defaults[name].unit_label)}`;
  return ORDER_TERMS.map(term).join(' · ');
}

// ── The form's own lines ────────────────────────────────────────────────────

/** A symbol the preview route takes: the server's own pattern for a US stock or ETF. */
const SYMBOL = /^[A-Z]{1,5}(?:[.-][A-Z])?$/;

/** What the What step still needs of an option bot, as its header says it, or `null`. */
export function optionsMissing(options: DeployOptionSettings): string | null {
  if (!SYMBOL.test(options.underlying)) return 'Needs a valid symbol';
  return options.legs.length === 0 ? 'Choose a structure' : null;
}

// ── The preview's question ──────────────────────────────────────────────────

/** What one preview asks about, beside the page's own key. */
export interface OptionPreviewSubject {
  readonly underlying: string;
  readonly structure: NamedStructure | null;
  readonly legs: readonly OptionLegRules[];
  /** What the bot listens to; only the lean warning reads it. */
  readonly listensTo: string;
  /** How many whole structures the bot opens at an entry. */
  readonly structures: number;
  readonly terms: OptionOrderTerms | null;
  /** The catalogue's order terms, whose ranges say whether `terms` can be sent; `null` until it is read. */
  readonly offered: OptionOrderTermDefaults | null;
}

/**
 * Why nothing is asked of the preview about what is on screen, in the page's
 * own words, or `null` when there is something to ask: a half-typed
 * underlying, a size that is no whole number, or order terms still unread or
 * holding what cannot be sent. Form chrome, said where the preview would be:
 * never one of the server's states.
 */
export function previewNotAsked(subject: OptionPreviewSubject): string | null {
  if (!SYMBOL.test(subject.underlying)) return 'Nothing is asked yet: “Options on” needs a symbol.';
  if (!isWholeCount(subject.structures)) return 'Nothing is asked yet: the position size in How is not a whole number of structures.';
  if (subject.terms === null || subject.offered === null) return 'Nothing is asked yet: the option order terms are still being read.';
  return orderTermsSet(subject.terms, subject.offered)
    ? null
    : 'Nothing is asked yet: an option order term in How holds a value that cannot be sent. Correct it there.';
}

/**
 * The preview request for what is on screen, with the owner's size and four
 * order terms, or `null` while {@link previewNotAsked} says why there is
 * nothing to ask.
 */
export function previewRequest(previewKey: string, subject: OptionPreviewSubject): OptionStructurePreviewRequest | null {
  const { terms } = subject;
  if (previewNotAsked(subject) !== null || !orderTermsSet(terms, subject.offered)) return null;
  return {
    preview_key: previewKey,
    underlying: subject.underlying,
    ...(SYMBOL.test(subject.listensTo) ? { listens_to: subject.listensTo } : {}),
    structure: subject.structure,
    legs: [...subject.legs],
    structures: subject.structures,
    gap_limit_percent: terms.gap_limit_percent,
    entry_wait_seconds: terms.entry_wait_seconds,
    cost_limit_percent: terms.cost_limit_percent,
    expiry_close_lead_minutes: terms.expiry_close_lead_minutes,
  };
}
