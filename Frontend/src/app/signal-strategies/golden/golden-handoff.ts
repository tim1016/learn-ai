import { computed, type Signal } from '@angular/core';

import type { PlanEdit } from '../../components/golden-search/golden-search-plan-draft';
import type { StrategyCapability } from '../../components/golden-search/golden-search.types';
import { rangeVaries, type ParamRange } from '../../shared/param-range/param-range';
import type { WorkspaceStateContent } from '../../shared/workspace/workspace-state';
import { injectBacktestHandoff, type HandoffState } from '../explore/handoff/backtest-handoff';
import type { ExploreHandoff, HandoffAccount, HandoffGap, HandoffSource } from '../explore/handoff/explore-handoff';
import { injectSweepHandoff } from '../explore/handoff/sweep-handoff';
import { injectWalkForwardHandoff } from '../explore/handoff/walk-forward-handoff';

/**
 * A window Explore already looked at. The plan's final interval is compared with
 * it: a final test on dates already viewed is not an untouched look, whatever the
 * recorded-research ledger says, so the plan never calls those dates unexposed.
 */
export interface ViewedWindow {
  /** First and last ET day, both inclusive. */
  readonly from: string;
  readonly to: string;
  /** The source's own words: "backtest run #41". */
  readonly source: string;
}

/**
 * What Explore hands a golden search plan (PRD #3490 R17). It is a plan to review,
 * never a study: `edits` are the same edits the plan form's own controls make,
 * applied once its defaults for `symbol` are read and then preflighted like any
 * edit. Nothing here locks a plan, starts a stage, picks the final interval or
 * carries an exploratory leader as a starting point.
 */
export interface GoldenHandoff extends HandoffAccount {
  readonly symbol: string;
  readonly edits: readonly PlanEdit[];
  readonly viewed: ViewedWindow;
}

export type GoldenHandoffState =
  | { readonly kind: 'loading' }
  | { readonly kind: 'ready'; readonly handoff: GoldenHandoff }
  | { readonly kind: 'refused'; readonly state: WorkspaceStateContent };

function sourceWords(source: HandoffSource): string {
  switch (source.kind) {
    case 'backtest':
      return `backtest run #${source.runId}`;
    case 'sweep':
      return `sweep ${source.searchId.slice(0, 8)}`;
    case 'walk-forward':
      return `walk-forward study ${source.studyId.slice(0, 8)}`;
  }
}

/** A knob's range as the plan's own number controls take it: raw text, so a bad value is the form's to refuse. */
function knobEdits(name: string, range: ParamRange): { readonly edits: PlanEdit[]; readonly low: number; readonly high: number; readonly collapsed: boolean } {
  if (!rangeVaries(range)) {
    const value = range.type === 'value_list' ? range.values[0] : range.low;
    return {
      edits: [
        { kind: 'knob-mode', name, mode: 'fixed' },
        { kind: 'knob-number', name, field: 'fixed_value', raw: String(value) },
      ],
      low: value,
      high: value,
      collapsed: false,
    };
  }
  if (range.type === 'low_high_step') {
    return {
      edits: [
        { kind: 'knob-mode', name, mode: 'search' },
        { kind: 'knob-number', name, field: 'low', raw: String(range.low) },
        { kind: 'knob-number', name, field: 'high', raw: String(range.high) },
        { kind: 'knob-number', name, field: 'step', raw: String(range.step) },
      ],
      low: range.low,
      high: range.high,
      collapsed: false,
    };
  }
  const low = Math.min(...range.values);
  const high = Math.max(...range.values);
  return {
    edits: [
      { kind: 'knob-mode', name, mode: 'search' },
      { kind: 'knob-number', name, field: 'low', raw: String(low) },
      { kind: 'knob-number', name, field: 'high', raw: String(high) },
    ],
    low,
    high,
    collapsed: true,
  };
}

/**
 * A golden search plan from what an Explore tool handed over. A grid becomes the
 * plan's knob ranges (a varied setting is searched over its span, a fixed one is
 * held), costs, capital and fill mode become its execution, and a walk-forward's
 * fold lengths become its fold lengths. A single backtest is one point, not a
 * range, so it carries only the symbol, costs and capital. A range wider than the
 * strategy's declared Golden Search domain is carried as it is and named: the plan
 * form's own check refuses it until it is narrowed, so nothing is clipped quietly.
 */
export function goldenHandoffOf(handoff: ExploreHandoff, capability: StrategyCapability): GoldenHandoff {
  const { spec, lineage } = handoff;
  const edits: PlanEdit[] = [];
  const unsupported: HandoffGap[] = [...handoff.unsupported];
  const toChoose: HandoffGap[] = [...handoff.toChoose];
  const carriedField = (field: string): boolean => handoff.carried.some((item) => item.field === field);

  if (lineage.source.kind !== 'backtest') {
    const declared = new Map(capability.knobs.map((knob) => [knob.name, knob]));
    for (const [name, range] of Object.entries(spec.param_ranges)) {
      const knob = declared.get(name);
      if (knob === undefined) {
        unsupported.push({
          field: `Setting ${name}`,
          reason: 'Golden Search declares no search for this setting, so the plan keeps the strategy’s declared value.',
        });
        continue;
      }
      const taken = knobEdits(name, range);
      edits.push(...taken.edits);
      if (taken.collapsed) {
        unsupported.push({
          field: knob.label,
          reason: `The source listed values; the plan searches their span, ${taken.low} to ${taken.high}, at Golden Search’s own step.`,
        });
      }
      if (taken.low < knob.domain_low || taken.high > knob.domain_high) {
        unsupported.push({
          field: knob.label,
          reason: `The source’s ${taken.low} to ${taken.high} lies outside the range Golden Search declares for it, ${knob.domain_low} to ${knob.domain_high}. It is carried as it is, and the plan will refuse it until you narrow it.`,
        });
      }
    }
    const untouched = capability.knobs.filter((knob) => spec.param_ranges[knob.name] === undefined);
    if (untouched.length > 0) {
      toChoose.push({
        field: 'Settings the source did not set',
        reason: `${untouched.map((knob) => knob.label).join(', ')} start at Golden Search’s own ranges.`,
      });
    }
  }

  if (carriedField('Commission per order')) edits.push({ kind: 'number', field: 'commission_per_order', raw: String(spec.commission_per_order) });
  if (carriedField('Slippage per share')) edits.push({ kind: 'number', field: 'slippage_per_share', raw: String(spec.slippage_per_share) });
  if (carriedField('Starting capital')) edits.push({ kind: 'number', field: 'initial_cash', raw: String(spec.initial_cash) });
  if (carriedField('Fill mode')) edits.push({ kind: 'fill-mode', fillMode: spec.fill_mode });
  if (handoff.folds !== undefined) {
    edits.push(
      { kind: 'number', field: 'training_months', raw: String(handoff.folds.trainingMonths) },
      { kind: 'number', field: 'test_months', raw: String(handoff.folds.testMonths) },
    );
  }

  return {
    lineage,
    destination: 'golden',
    carried: handoff.carried,
    unsupported,
    toChoose,
    symbol: spec.symbol,
    edits,
    viewed: { from: lineage.from, to: lineage.to, source: sourceWords(lineage.source) },
  };
}

/** Whether the explored window and an ET day span (both inclusive) share a day. ISO dates order as text. */
export function windowsOverlap(viewed: Pick<ViewedWindow, 'from' | 'to'>, from: string, to: string): boolean {
  return viewed.from <= to && viewed.to >= from;
}

function convert(state: HandoffState | null, capability: () => StrategyCapability | null): GoldenHandoffState | null {
  if (state === null) return null;
  if (state.kind !== 'ready') return state;
  const declaration = capability();
  if (declaration === null) {
    return {
      kind: 'refused',
      state: {
        kind: 'unavailable',
        happened: 'Golden Search declares nothing for this strategy, so there is no plan to fill in.',
        why: 'Its search ranges, steps and constraints come from that declaration.',
        next: 'The plan starts empty. Explore the strategy further, or choose another one.',
      },
    };
  }
  return { kind: 'ready', handoff: goldenHandoffOf(state.handoff, declaration) };
}

/**
 * The handoff Golden's address names, from the Explore evidence it points at:
 * `?walkForward=` over `?search=` over `?run=`, as Walk-forward ranks a search
 * over a run (a grid already is a range). `null` when the address names none;
 * otherwise loading, ready or refused in words. Call it from a field initializer
 * inside a strategy's Explore scope.
 */
export function injectGoldenHandoff(
  run: Signal<string | undefined>,
  search: Signal<string | undefined>,
  walkForward: Signal<string | undefined>,
  capability: Signal<StrategyCapability | null>,
): Signal<GoldenHandoffState | null> {
  const fromBacktest = injectBacktestHandoff('golden', run);
  const fromSweep = injectSweepHandoff(search, 'golden');
  const fromWalkForward = injectWalkForwardHandoff(walkForward);
  return computed(() => convert(fromWalkForward() ?? fromSweep() ?? fromBacktest(), capability));
}
