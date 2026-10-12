import type { ValidationDetail } from './options-backtest.service';

/** Problems by the field they belong to. The keys are `FIELD` names, or `legKey` for a field of one leg. */
export type FieldProblems = ReadonlyMap<string, string>;

export const NO_PROBLEMS: FieldProblems = new Map();

export const FIELD = {
  underlying: 'underlying',
  entry: 'entry',
  exit: 'exit',
  noReentry: 'noReentry',
  squareOff: 'squareOff',
  legs: 'legs',
  slippage: 'slippage',
  cash: 'cash',
  fees: 'fees',
  perContractFee: 'perContractFee',
  name: 'name',
} as const;

export type LegField =
  | 'leg'
  | 'contracts'
  | 'expiry'
  | 'strike'
  | 'stopLoss'
  | 'stopLossValue'
  | 'momentum'
  | 'momentumValue'
  | 'reentry'
  | 'reentryCount';

export function legKey(legId: string, field: LegField): string {
  return `leg:${legId}:${field}`;
}

/** What is wrong with a setup: by field, and what has no field of its own. */
export interface SetupProblems {
  readonly fields: FieldProblems;
  readonly other: readonly string[];
}

export const NO_SETUP_PROBLEMS: SetupProblems = { fields: NO_PROBLEMS, other: [] };

/**
 * The server's field errors, placed at the fields they name. `legIds` are the
 * ids of the legs of the body that was sent, because `loc` counts legs by
 * position. An error that names no field the page has is kept, in `other`:
 * nothing the server says is dropped.
 */
export function problemsFromDetails(details: readonly ValidationDetail[], legIds: readonly string[]): SetupProblems {
  const fields = new Map<string, string>();
  const other: string[] = [];
  for (const detail of details) {
    const key = keyOf(detail.loc, legIds);
    const message = cleanMessage(detail.msg);
    if (key === null) other.push(describeDetail(detail));
    else fields.set(key, fields.has(key) ? `${fields.get(key)} ${message}` : message);
  }
  return { fields, other };
}

/** `legs.0.contracts: Input should be...` for a reason that is read in a list, such as an import's. */
export function describeDetail(detail: ValidationDetail): string {
  const where = detail.loc.filter((part) => part !== 'body').join('.');
  return where === '' ? cleanMessage(detail.msg) : `${where}: ${cleanMessage(detail.msg)}`;
}

/** Pydantic prefixes a rule written in the schema with "Value error, "; the rule is the message. */
export function cleanMessage(message: string): string {
  return message.replace(/^Value error, /, '');
}

function keyOf(loc: readonly (string | number)[], legIds: readonly string[]): string | null {
  if (loc[0] === 'path' && loc[1] === 'name') return FIELD.name;
  const [head, ...rest] = loc[0] === 'body' ? loc.slice(1) : loc;
  switch (head) {
    case 'underlying':
      return FIELD.underlying;
    case 'entry':
      return FIELD.entry;
    case 'exit':
      return FIELD.exit;
    case 'no_reentry_after':
      return FIELD.noReentry;
    case 'square_off':
      return FIELD.squareOff;
    case 'slippage_percent':
      return FIELD.slippage;
    case 'starting_cash_usd':
      return FIELD.cash;
    case 'fees':
      return rest.includes('per_contract_usd') ? FIELD.perContractFee : FIELD.fees;
    case 'legs':
      return legKeyOf(rest, legIds);
    default:
      return null;
  }
}

function legKeyOf(rest: readonly (string | number)[], legIds: readonly string[]): string {
  const [position, ...inside] = rest;
  const legId = typeof position === 'number' ? legIds[position] : undefined;
  if (legId === undefined) return FIELD.legs;
  return legKey(legId, legFieldOf(inside));
}

/** `inside` is what follows `legs.N`; a union adds its tag (`expiry.min_days.days`), so the field is found by name. */
function legFieldOf(inside: readonly (string | number)[]): LegField {
  const [field] = inside;
  switch (field) {
    case 'contracts':
      return 'contracts';
    case 'expiry':
      return 'expiry';
    case 'strike':
      return 'strike';
    case 'stop_loss':
      return inside.includes('value') ? 'stopLossValue' : 'stopLoss';
    case 'momentum':
      return inside.includes('value') ? 'momentumValue' : 'momentum';
    case 'reentry_on_stop':
      return inside.includes('count') ? 'reentryCount' : 'reentry';
    default:
      return 'leg';
  }
}
