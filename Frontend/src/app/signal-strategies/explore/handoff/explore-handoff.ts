import type { GridSearchDetail, GridSearchSpecRequest } from '../../../components/grid-search/grid-search.types';
import type { StrategyLabRunInputs, StrategyInfo } from '../../../components/strategy-lab/strategy-lab.models';
import type { WalkForwardStudyDetail } from '../../../components/walk-forward-study/walk-forward-study.types';
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
export type HandoffDestination = Extract<ExploreTabId, 'sweep' | 'walk-forward'> | 'golden';

/** Where the handoff came from, by the source's own native id. */
export type HandoffSource =
  | { readonly kind: 'backtest'; readonly runId: number }
  | { readonly kind: 'sweep'; readonly searchId: string }
  | { readonly kind: 'walk-forward'; readonly studyId: string };

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

/** What a handoff notice tells: where it came from, what arrived, what did not and what is left to choose. */
export interface HandoffAccount {
  readonly lineage: HandoffLineage;
  readonly destination: HandoffDestination;
  readonly carried: readonly HandoffCarried[];
  /** Fields of the source the destination cannot take. Never silently dropped. */
  readonly unsupported: readonly HandoffGap[];
  /** Settings only the destination has, left for the owner. */
  readonly toChoose: readonly HandoffGap[];
}

export interface ExploreHandoff extends HandoffAccount {
  /** What the destination's form opens on. The form still preflights it. */
  readonly spec: GridSearchSpecRequest;
  /** The fold lengths a walk-forward source used, for a destination that has folds of its own. */
  readonly folds?: { readonly trainingMonths: number; readonly testMonths: number };
}

/** What each destination is called in the words of what it cannot take. */
const DESTINATION_WORDS: Readonly<Record<HandoffDestination, string>> = {
  sweep: 'Sweep',
  'walk-forward': 'Walk-forward',
  golden: 'Golden Search',
};

/** The destination's own settings a source cannot have chosen. */
export function destinationChoices(destination: HandoffDestination): HandoffGap[] {
  if (destination === 'golden') {
    return [
      {
        field: 'Final test dates',
        reason: 'Explore never chooses the final interval. The plan lays its own dates out from the lake and says whether the window Explore already viewed overlaps them.',
      },
      {
        field: 'Objective, trade floors and audits',
        reason: 'An in-sample ranking is not a choice for the final decision, so these start at Golden Search’s own values.',
      },
    ];
  }
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
  const target = DESTINATION_WORDS[destination];

  carried.push({ field: 'Strategy', value: `${strategy.display_name} (${entry.strategy_key}, program ${entry.program_version ?? 'none'})` });
  if (run.customSource) {
    unsupported.push({
      field: 'Algorithm source',
      reason: `The run executed LEAN source pasted into the workbench, which the record does not keep. ${target} runs ${strategy.display_name}’s registered program, so what it finds is not that code’s result; the symbol, window and costs below are as the run recorded them.`,
    });
  }
  carried.push({ field: 'Symbol', value: inputs.range.symbol });
  if (destination === 'golden') {
    unsupported.push({
      field: 'Window',
      reason: `The run viewed ${inputs.range.from} to ${inputs.range.to}. A golden search lays its own dates out and never takes an explored window as its study range; the plan marks this one as already viewed.`,
    });
  } else {
    carried.push({ field: 'Window', value: `${inputs.range.from} to ${inputs.range.to}` });
  }

  const sourceBars = barsOf(inputs);
  const supportedBars = strategy.supported_resolutions;
  // A golden search reads minute bars only; Sweep and Walk-forward take what the strategy supports.
  const resolution: 'minute' | 'daily' =
    destination === 'golden'
      ? 'minute'
      : sourceBars !== null && supportedBars.includes(sourceBars) ? sourceBars : supportedBars.includes('daily') && !supportedBars.includes('minute') ? 'daily' : 'minute';
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
  const usedText = used.length === 0 ? 'the strategy’s defaults' : used.map((name) => `${name} = ${String(inputs.parameters[name])}`).join(', ');
  if (destination === 'golden') {
    unsupported.push({
      field: 'Settings',
      reason: `The run used one point (${usedText}). A golden search explores ranges from its own incumbent, so one exploratory point is carried neither as a range nor as a starting point.`,
    });
  } else {
    carried.push({ field: 'Settings', value: usedText });
  }
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
      reason: `The run used ${engineWords(inputs.engine)}; fees were charged per fill by the engine’s own model and no flat commission was recorded. ${target} takes a flat commission per order, so ${destination === 'golden' ? 'the plan keeps Golden Search’s own commission' : 'it starts at 0'}.`,
    });
  } else {
    carried.push({ field: 'Commission per order', value: String(inputs.commissionPerOrder) });
  }
  unsupported.push({
    field: 'Slippage',
    reason: `A saved run report does not carry its slippage; ${destination === 'golden' ? 'the plan keeps Golden Search’s own slippage' : `${target} starts at 0 per share`}.`,
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

/** The grid a request describes in the words a handoff carries, and the ET dates of the window it covered. */
function gridOf(request: GridSearchSpecRequest, strategy: StrategyInfo | null, entry: StrategyRegistryEntry, destination: HandoffDestination) {
  const from = etIsoDate(request.start_ms);
  const to = etIsoDate(request.end_ms - 1);
  const varied = Object.entries(request.param_ranges).filter(([, range]) => rangeVaries(range));
  const fixed = Object.entries(request.param_ranges).filter(([, range]) => !rangeVaries(range));
  const golden = destination === 'golden';

  const carried: HandoffCarried[] = [
    { field: 'Strategy', value: `${strategy?.display_name ?? entry.display_name} (${entry.strategy_key}, program ${entry.program_version ?? 'none'})` },
    { field: 'Symbol', value: request.symbol },
    // A golden search lays its own dates out: the window is the lineage's, and is not carried as the study range.
    ...(golden ? [] : [{ field: 'Window', value: `${from} to ${to}` }, { field: 'Resolution', value: request.resolution }]),
    {
      field: 'Grid',
      value: varied.length === 0 ? 'no setting varied' : varied.map(([name, range]) => rangeText(name, range)).join('; '),
    },
    ...(fixed.length === 0 ? [] : [{ field: 'Fixed settings', value: fixed.map(([name, range]) => rangeText(name, range)).join('; ') }]),
    { field: 'Fill mode', value: isFillModeName(request.fill_mode) ? fillModeLabel(request.fill_mode) : String(request.fill_mode) },
    { field: 'Commission per order', value: String(request.commission_per_order) },
    { field: 'Slippage per share', value: String(request.slippage_per_share) },
    { field: 'Starting capital', value: String(request.initial_cash) },
    ...(golden ? [] : [{ field: 'Ranking', value: `${request.measure}, at least ${request.min_trades} trades` }]),
    { field: 'Session and adjustment', value: `regular session, split-adjusted (${golden ? 'every Explore tool and Golden Search read' : 'both tools read'} only these)` },
  ];

  const unsupported: HandoffGap[] = [];
  if (golden) {
    unsupported.push(
      {
        field: 'Window',
        reason: `The source viewed ${from} to ${to}. A golden search lays its own dates out and never takes an explored window as its study range; the plan marks this one as already viewed.`,
      },
      {
        field: 'Ranking',
        reason: `The source ranked by ${request.measure} with at least ${request.min_trades} trades, in-sample. Golden Search starts at its own objective and trade floor.`,
      },
    );
  }
  return { from, to, carried, unsupported };
}

/**
 * A Grid Search's request as the next tool's starting form: the same grid, window,
 * costs and ranking for Walk-forward, to be tested out of sample, or the same
 * ranges, costs and capital for a golden search plan. The search's own results do
 * not travel: a walk-forward picks a winner afresh on every training window, and
 * a golden search never starts from, or is shown, an in-sample leader.
 */
export function handoffFromSweep(input: {
  readonly search: GridSearchDetail;
  readonly strategy: StrategyInfo | null;
  readonly entry: StrategyRegistryEntry;
  readonly destination?: HandoffDestination;
}): ExploreHandoff {
  const { search, strategy, entry, destination = 'walk-forward' } = input;
  const request = search.request;
  const { from, to, carried, unsupported } = gridOf(request, strategy, entry, destination);

  unsupported.unshift({
    field: 'Leader and cell results',
    reason:
      destination === 'golden'
        ? 'The sweep’s results are in-sample. A golden search never starts from the sweep’s leader, and its final test is not shown it.'
        : 'The sweep’s results are in-sample. A walk-forward picks its own winner on each training window, so the sweep’s leader is not an input.',
  });
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
    destination,
    spec: request,
    carried,
    unsupported,
    toChoose:
      destination === 'golden'
        ? destinationChoices('golden')
        : [{ field: 'Training and test lengths', reason: 'A walk-forward cuts the window into folds; choose how many months train and how many test.' }],
  };
}

/**
 * A walk-forward study's request as a golden search plan: its grid, costs, capital
 * and fold lengths. The study's folds, winners and verdict are in-sample evidence
 * about a selection procedure and do not travel: a golden search repeats its own
 * procedure on its own windows and opens one final test it has not shown.
 */
export function handoffFromWalkForward(input: {
  readonly study: WalkForwardStudyDetail;
  readonly strategy: StrategyInfo | null;
  readonly entry: StrategyRegistryEntry;
}): ExploreHandoff {
  const { study, strategy, entry } = input;
  const request = study.request;
  const { from, to, carried, unsupported } = gridOf(request, strategy, entry, 'golden');
  carried.push(
    { field: 'Training window', value: `${request.training_months} months` },
    { field: 'Test window', value: `${request.test_months} months` },
  );
  unsupported.unshift({
    field: 'Folds, winners and verdict',
    reason: 'The study’s folds and its verdict describe how a selection procedure held up on those windows. A golden search runs its own nested folds and never starts from the study’s winners.',
  });
  if (study.incomplete) {
    unsupported.push({
      field: 'Unfinished folds',
      reason: `The study is ${study.status} with ${study.fold_count - study.completed_folds - study.failed_folds} folds not completed; the grid and fold lengths travel, not its partial results.`,
    });
  }
  return {
    lineage: {
      source: { kind: 'walk-forward', studyId: study.id },
      strategyKey: entry.strategy_key,
      programVersion: entry.program_version,
      from,
      to,
    },
    destination: 'golden',
    spec: request,
    folds: { trainingMonths: request.training_months, testMonths: request.test_months },
    carried,
    unsupported,
    toChoose: destinationChoices('golden'),
  };
}
