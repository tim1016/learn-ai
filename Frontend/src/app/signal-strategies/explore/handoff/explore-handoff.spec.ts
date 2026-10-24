import { describe, expect, it } from 'vitest';

import { inputsFromSavedRun } from '../../../components/strategy-lab/strategy-lab.models';
import { etDayEndMs, etMidnightMs } from '../../../shared/date/et-midnight';
import { registryEntry } from '../../testing/fixtures';
import { EMA, SAVED_RUN_POLICY, savedRun, savedSearch, sweepRequest, sweepableEma } from '../testing/explore-fixtures';
import { handoffFromBacktest, handoffFromSweep } from './explore-handoff';

const NO_RANGE = { symbol: '', from: '', to: '', resolution: 'minute' } as const;

function fromRun(overrides: Parameters<typeof savedRun>[0] = {}, destination: 'sweep' | 'walk-forward' = 'sweep') {
  const run = savedRun(overrides);
  // The strategy also declares a text setting, which a numeric grid cannot hold.
  const declared = sweepableEma();
  const strategy = {
    ...declared,
    params_schema: { ...declared.params_schema, properties: { ...declared.params_schema.properties, direction: { type: 'string', default: 'long' } } },
  };
  return handoffFromBacktest({ run, inputs: inputsFromSavedRun(run, NO_RANGE), strategy, entry: registryEntry(), destination });
}

const carried = (handoff: ReturnType<typeof fromRun>, field: string) => handoff.carried.find((item) => item.field === field)?.value;
const gap = (handoff: ReturnType<typeof fromRun>, field: string) => handoff.unsupported.find((item) => item.field === field)?.reason;

describe('a handoff from a backtest', () => {
  it('carries the strategy, symbol, settings, window, fill, costs and capital as a request, each setting fixed at the run’s value', () => {
    const handoff = fromRun();

    expect(handoff.spec).toEqual({
      strategy_key: EMA,
      symbol: 'QQQ',
      param_ranges: {
        fast_period: { type: 'value_list', values: [8] },
        slow_period: { type: 'value_list', values: [21] },
        // The run did not set it, so the strategy's own default stands.
        gap: { type: 'value_list', values: [0.2] },
      },
      start_ms: etMidnightMs('2026-03-02'),
      end_ms: etDayEndMs('2026-04-02'),
      resolution: 'minute',
      fill_mode: 'next_bar_open',
      commission_per_order: 0.35,
      slippage_per_share: 0,
      initial_cash: 75_000,
      measure: 'sharpe_ratio',
      min_trades: 5,
    });
    expect(handoff.lineage).toEqual({
      source: { kind: 'backtest', runId: 91 },
      strategyKey: EMA,
      programVersion: 'ema-crossover-signal/v2',
      from: '2026-03-02',
      to: '2026-04-02',
    });
    expect(carried(handoff, 'Strategy')).toContain('program ema-crossover-signal/v2');
    expect(carried(handoff, 'Session')).toContain('regular');
    expect(carried(handoff, 'Adjustment')).toContain('split-adjusted');
  });

  it('says a saved run carries no slippage rather than inventing one', () => {
    expect(gap(fromRun(), 'Slippage')).toContain('does not carry its slippage');
  });

  it('names the engine, the raw bars and the extended session a sweep cannot take, and applies none of them', () => {
    const handoff = fromRun({
      requestedEngine: 'both',
      commissionPerOrder: null,
      dataPolicy: { ...SAVED_RUN_POLICY, adjusted: false, session: 'extended' },
    });

    expect(gap(handoff, 'Engine')).toContain('both engines');
    expect(gap(handoff, 'Adjustment')).toContain('raw bars');
    expect(gap(handoff, 'Session')).toContain('extended hours');
    expect(gap(handoff, 'Commission')).toContain('starts at 0');
    expect(handoff.spec.commission_per_order).toBe(0);
    expect(carried(handoff, 'Adjustment')).toBeUndefined();
    expect(carried(handoff, 'Session')).toBeUndefined();
  });

  it('flags a setting that is not a number, and a fixture replay, instead of dropping them', () => {
    const handoff = fromRun({
      parameters: JSON.stringify({ symbol: 'QQQ', fast_period: 8, direction: 'long', retired: 3 }),
      dataPolicy: { ...SAVED_RUN_POLICY, provider_kind: 'fixture', fixture_id: 'bar-store-v1-x' },
    });

    expect(gap(handoff, 'Setting direction')).toContain('Sweep grids hold numbers only');
    expect(gap(handoff, 'Setting retired')).toContain('no longer declares');
    expect(gap(handoff, 'Data')).toContain('bar-store-v1-x');
  });

  it('flags a fill mode a sweep does not offer and starts at the research default', () => {
    const handoff = fromRun({ fillMode: 'lean-sidecar' });

    expect(gap(handoff, 'Fill mode')).toContain('"lean-sidecar"');
    expect(handoff.spec.fill_mode).toBe('decision_minute_open');
  });

  it('leaves what only the destination has to the owner, naming the walk-forward lengths for a walk-forward', () => {
    expect(fromRun().toChoose.map((item) => item.field)).toEqual(['Ranking measure and minimum trades', 'Which settings to vary']);
    expect(fromRun({}, 'walk-forward').toChoose.map((item) => item.field)).toEqual(['Ranking measure and minimum trades', 'Training and test lengths']);
  });
});

describe('a handoff from a sweep', () => {
  it('carries the whole request unchanged, and the lineage of the search it came from', () => {
    const search = savedSearch();
    const handoff = handoffFromSweep({ search, strategy: sweepableEma(), entry: registryEntry() });

    expect(handoff.spec).toBe(search.request);
    expect(handoff.destination).toBe('walk-forward');
    expect(handoff.lineage).toEqual({
      source: { kind: 'sweep', searchId: 'grid-search-0001' },
      strategyKey: EMA,
      programVersion: 'ema-crossover-signal/v2',
      from: '2026-01-02',
      to: '2026-06-30',
    });
    expect(handoff.carried.find((item) => item.field === 'Grid')?.value).toBe('fast_period 5 to 9 step 2');
    expect(handoff.carried.find((item) => item.field === 'Fixed settings')?.value).toBe('slow_period = 21; gap = 0.2');
    expect(handoff.carried.find((item) => item.field === 'Slippage per share')?.value).toBe('0.01');
  });

  it('does not carry the leader into an out-of-sample test, and says why', () => {
    const handoff = handoffFromSweep({ search: savedSearch(), strategy: sweepableEma(), entry: registryEntry() });

    expect(handoff.unsupported.map((item) => item.field)).toEqual(['Leader and cell results']);
    expect(handoff.unsupported[0].reason).toContain('in-sample');
    expect(handoff.toChoose.map((item) => item.field)).toEqual(['Training and test lengths']);
  });

  it('says an unfinished sweep travels as a grid only', () => {
    const handoff = handoffFromSweep({
      search: savedSearch({ status: 'interrupted', incomplete: true, completed_cells: 1, expected_cells: 3, request: sweepRequest() }),
      strategy: null,
      entry: registryEntry(),
    });

    expect(handoff.unsupported.find((item) => item.field === 'Unfinished cells')?.reason).toContain('2 cells missing');
  });
});
