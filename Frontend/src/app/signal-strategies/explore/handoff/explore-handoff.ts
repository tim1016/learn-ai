import type { GridSearchDetail, GridSearchSpecRequest } from '../../../components/grid-search/grid-search.types';
import type { StrategyLabRunInputs, StrategyInfo } from '../../../components/strategy-lab/strategy-lab.models';
import { RESEARCH_DEFAULT_FILL_MODE, fillModeLabel, isFillModeName } from '../../../models/fill-mode';
import type { BacktestRunDetail } from '../../../services/backtest-runs.types';
import { etDayEndMs, etIsoDate, etMidnightMs } from '../../../shared/date/et-midnight';
import { defaultNumericValue, numericStrategyParams, rangeVaries, type ParamRange } from '../../../shared/param-range/param-range';
import type { StrategyRegistryEntry } from '../../signal-strategy-evidence';
import type { ExploreTabId } from '../explore-tabs';

/**
 * A handoff between Explore's tools (PRD #3490 R17): what the owner was looking at
 * in one tool, offered to the next as a form to review.
 *
 * It carries what the next tool can take, says in words what it cannot and why,
 * and names what is left to choose. It never starts anything: the destination
 * opens its form on `spec`, checks it against its own schema as it checks any
 * edit, and runs only when the owner presses its run control.
 */
export type HandoffDestination = Extract<ExploreTabId, 'sweep' | 'walk-forward'>;

/** Where the handoff came from, by the source's own native id. */
export type HandoffSource =
  | { readonly kind: 'backtest'; readonly runId: number }
  | { readonly kind: 'sweep'; readonly searchId: string };

/** Which strategy and which evidence a handoff stands on. */
export interface HandoffLineage {
  readonly source: HandoffSource;
  readonly strategyKey: string;
  /** The registry's program version for the strategy: the code the destination will run. */
  readonly programVersion: string | null;
  /** The window the source covered, as the ET dates it states, both inclusive. */
  readonly from: string;
  readonly to: string;
}

/** A field that arrived, as the destination shows it. */
export interface HandoffCarried {
  readonly field: string;
  readonly value: string;
}

/** A field that did not arrive, and why. */
export interface HandoffGap {
  readonly field: string;
  readonly reason: string;
}

export interface ExploreHandoff {
  readonly lineage: HandoffLineage;
  readonly destination: HandoffDestination;
  /** What the destination's form opens on. The form still preflights it. */
  readonly spec: GridSearchSpecRequest;
  readonly carried: readonly HandoffCarried[];
  /** Fields of the source the destination cannot take. Never silently dropped. */
  readonly unsupported: readonly HandoffGap[];
  /** Settings only the destination has, left for the owner. */
  readonly toChoose: readonly HandoffGap[];
}

/** The destination's own settings a source cannot have chosen. */
function destinationChoices(destination: HandoffDestination): HandoffGap[] {
  const shared: HandoffGap[] = [
    { field: 'Ranking measure and minimum trades', reason: 'The source does not rank settings, so these start at the form’s own values.' },
  ];
  return destination === 'walk-forward'
    ? [...shared, { field: 'Training and test lengths', reason: 'A walk-forward cuts the window into folds; choose how many months train and how many test.' }]
    : [...shared, { field: 'Which settings to vary', reason: 'Every setting starts fixed at the source’s value; tick the ones the grid should vary.' }];
}

function barsOf(inputs: StrategyLabRunInputs): 'minute' | 'daily' | null {
  const resolution = inputs.range.resolution;
  return resolution === 'minute' || resolution === 'daily' ? resolution : null;
}

function engineWords(engine: StrategyLabRunInputs['engine']): string {
  return engine === 'lean' ? 'the LEAN engine' : engine === 'both' ? 'both engines' : 'the Python engine';
}

/**
 * A saved backtest as the next tool's starting form.
 *
 * Sweep and Walk-forward read split-adjusted regular-session bars from the lake
 * with the Python engine, one flat commission and no slippage the run recorded.
 * A run that differs in any of those still hands over everything else, and the
 * difference is listed, not applied.
 */
export function handoffFromBacktest(input: {
  readonly run: BacktestRunDetail;
  /** The run's own inputs, read back by `inputsFromSavedRun`. */
  readonly inputs: StrategyLabRunInputs;
  readonly strategy: StrategyInfo;
  readonly entry: StrategyRegistryEntry;
  readonly destination: HandoffDestination;
}): ExploreHandoff {
  const { run, inputs, strategy, entry, destination } = input;
  const policy = inputs.dataPolicy;
  const carried: HandoffCarried[] = [];
  const unsupported: HandoffGap[] = [];
  const target = destination === 'sweep' ? 'Sweep' : 'Walk-forward';

  carried.push({ field: 'Strategy', value: `${strategy.display_name} (${entry.strategy_key}, program ${entry.program_version ?? 'none'})` });
  carried.push({ field: 'Symbol', value: inputs.range.symbol });
  carried.push({ field: 'Window', value: `${inputs.range.from} to ${inputs.range.to}` });

  const sourceBars = barsOf(inputs);
  const supportedBars = strategy.supported_resolutions;
  const resolution: 'minute' | 'daily' =
    sourceBars !== null && supportedBars.includes(sourceBars) ? sourceBars : supportedBars.includes('daily') && !supportedBars.includes('minute') ? 'daily' : 'minute';
  if (sourceBars === resolution) {
    carried.push({ field: 'Resolution', value: resolution });
  } else {
    unsupported.push({
      field: 'Resolution',
      reason: `The run read ${String(inputs.range.resolution)} bars, which ${target} does not take for this strategy; it starts on ${resolution} bars.`,
    });
  }

  const paramRanges: Record<string, ParamRange> = {};
  const numeric = new Set<string>();
  for (const [name, property] of numericStrategyParams(strategy)) {
    numeric.add(name);
    const used = inputs.parameters[name];
    const value = typeof used === 'number' && Number.isFinite(used) ? used : defaultNumericValue(property);
    paramRanges[name] = { type: 'value_list', values: [value] };
  }
  const used = [...numeric].filter((name) => typeof inputs.parameters[name] === 'number');
  carried.push({
    field: 'Settings',
    value: used.length === 0 ? 'the strategy’s defaults' : used.map((name) => `${name} = ${String(inputs.parameters[name])}`).join(', '),
  });
  const properties = strategy.params_schema.properties ?? {};
  for (const [name, value] of Object.entries(inputs.parameters)) {
    if (name === 'symbol' || numeric.has(name)) continue;
    const declared = properties[name];
    unsupported.push({
      field: `Setting ${name}`,
      reason:
        declared === undefined
          ? `The run used ${JSON.stringify(value)}, but the strategy no longer declares this setting.`
          : `The run used ${JSON.stringify(value)}, which is not a number; ${target} grids hold numbers only, so the strategy’s default applies.`,
    });
  }

  const fillName = isFillModeName(run.fillMode) ? run.fillMode : RESEARCH_DEFAULT_FILL_MODE;
  if (isFillModeName(run.fillMode)) {
    carried.push({ field: 'Fill mode', value: fillModeLabel(run.fillMode) });
  } else {
    unsupported.push({
      field: 'Fill mode',
      reason: `The run recorded "${run.fillMode}", which is not a fill mode ${target} offers; it starts at ${fillModeLabel(fillName)}.`,
    });
  }

  // Only a Python run charges the flat commission the rail states; LEAN and a pair charge their own fee models per fill.
  const flatCommission = inputs.engine === 'python';
  if (!flatCommission) {
    unsupported.push({
      field: 'Commission',
      reason: `The run used ${engineWords(inputs.engine)}; fees were charged per fill by the engine’s own model and no flat commission was recorded. ${target} takes a flat commission per order, so it starts at 0.`,
    });
  } else {
    carried.push({ field: 'Commission per order', value: String(inputs.commissionPerOrder) });
  }
  unsupported.push({
    field: 'Slippage',
    reason: `A saved run report does not carry its slippage; ${target} starts at 0 per share.`,
  });
  carried.push({ field: 'Starting capital', value: String(inputs.initialCash) });

  if (inputs.engine !== 'python') {
    unsupported.push({
      field: 'Engine',
      reason: `The run used ${engineWords(inputs.engine)}; ${target} runs the Python engine only.`,
    });
  }
  if (policy?.session === 'extended') {
    unsupported.push({ field: 'Session', reason: `The run read extended hours; ${target} reads the regular session only.` });
  } else {
    carried.push({ field: 'Session', value: 'regular (the only session it reads)' });
  }
  if (policy?.adjusted === false) {
    unsupported.push({ field: 'Adjustment', reason: `The run read raw bars; ${target} reads split-adjusted bars only.` });
  } else {
    carried.push({ field: 'Adjustment', value: 'split-adjusted (the only tree it reads)' });
  }
  if (policy?.provider_kind === 'fixture') {
    unsupported.push({
      field: 'Data',
      reason: `The run replayed a frozen fixture (${policy.fixture_id ?? 'unidentified'}); ${target} reads the lake as it stands.`,
    });
  }

  return {
    lineage: {
      source: { kind: 'backtest', runId: run.id },
      strategyKey: entry.strategy_key,
      programVersion: entry.program_version,
      from: inputs.range.from,
      to: inputs.range.to,
    },
    destination,
    spec: {
      strategy_key: strategy.name,
      symbol: inputs.range.symbol,
      param_ranges: paramRanges,
      start_ms: etMidnightMs(inputs.range.from),
      end_ms: etDayEndMs(inputs.range.to),
      resolution,
      fill_mode: fillName,
      commission_per_order: flatCommission ? inputs.commissionPerOrder : 0,
      slippage_per_share: 0,
      initial_cash: inputs.initialCash,
      measure: 'sharpe_ratio',
      min_trades: 5,
    },
    carried,
    unsupported,
    toChoose: destinationChoices(destination),
  };
}

/** A varied or fixed range as the owner reads it. */
function rangeText(name: string, range: ParamRange): string {
  return range.type === 'low_high_step'
    ? `${name} ${range.low} to ${range.high} step ${range.step}`
    : `${name} ${range.values.length === 1 ? `= ${range.values[0]}` : `in ${range.values.join(', ')}`}`;
}

/**
 * A Grid Search's request as Walk-forward's starting form: the same grid, window,
 * costs and ranking, to be tested out of sample. The search's own results do not
 * travel: a walk-forward picks a winner afresh on every training window.
 */
export function handoffFromSweep(input: {
  readonly search: GridSearchDetail;
  readonly strategy: StrategyInfo | null;
  readonly entry: StrategyRegistryEntry;
}): ExploreHandoff {
  const { search, strategy, entry } = input;
  const request = search.request;
  const from = etIsoDate(request.start_ms);
  const to = etIsoDate(request.end_ms - 1);
  const varied = Object.entries(request.param_ranges).filter(([, range]) => rangeVaries(range));
  const fixed = Object.entries(request.param_ranges).filter(([, range]) => !rangeVaries(range));

  const carried: HandoffCarried[] = [
    { field: 'Strategy', value: `${strategy?.display_name ?? entry.display_name} (${entry.strategy_key}, program ${entry.program_version ?? 'none'})` },
    { field: 'Symbol', value: request.symbol },
    { field: 'Window', value: `${from} to ${to}` },
    { field: 'Resolution', value: request.resolution },
    {
      field: 'Grid',
      value: varied.length === 0 ? 'no setting varied' : varied.map(([name, range]) => rangeText(name, range)).join('; '),
    },
    ...(fixed.length === 0 ? [] : [{ field: 'Fixed settings', value: fixed.map(([name, range]) => rangeText(name, range)).join('; ') }]),
    { field: 'Fill mode', value: isFillModeName(request.fill_mode) ? fillModeLabel(request.fill_mode) : String(request.fill_mode) },
    { field: 'Commission per order', value: String(request.commission_per_order) },
    { field: 'Slippage per share', value: String(request.slippage_per_share) },
    { field: 'Starting capital', value: String(request.initial_cash) },
    { field: 'Ranking', value: `${request.measure}, at least ${request.min_trades} trades` },
    { field: 'Session and adjustment', value: 'regular session, split-adjusted (both tools read only these)' },
  ];

  const unsupported: HandoffGap[] = [
    {
      field: 'Leader and cell results',
      reason: 'The sweep’s results are in-sample. A walk-forward picks its own winner on each training window, so the sweep’s leader is not an input.',
    },
  ];
  if (search.incomplete) {
    unsupported.push({
      field: 'Unfinished cells',
      reason: `The sweep is ${search.status} with ${search.expected_cells - search.completed_cells - search.failed_cells} cells missing; the grid travels, not its partial results.`,
    });
  }

  return {
    lineage: {
      source: { kind: 'sweep', searchId: search.id },
      strategyKey: entry.strategy_key,
      programVersion: entry.program_version,
      from,
      to,
    },
    destination: 'walk-forward',
    spec: request,
    carried,
    unsupported,
    toChoose: [
      { field: 'Training and test lengths', reason: 'A walk-forward cuts the window into folds; choose how many months train and how many test.' },
    ],
  };
}
