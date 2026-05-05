/**
 * Pure immutable update helpers for ``StrategySpec``.
 *
 * The form-driven builder keeps a ``signal<StrategySpec>`` and writes
 * back via these helpers so each event handler is a one-liner. Everything
 * here returns a new spec — never mutates the input — which keeps the
 * Angular signal-equality cheap and the change detection predictable.
 *
 * The helpers don't validate. Re-validation happens via the Pydantic
 * schema at Run time when the spec round-trips through the backend.
 */

import {
  Condition,
  EntryBlock,
  ExitBlock,
  IndicatorBlock,
  LogicNode,
  StrategySpec,
  SurvivalAction,
  SurvivalRule,
} from '../../graphql/spec-strategy-types';

// ---------------------------------------------------------------------------
// Indicators
// ---------------------------------------------------------------------------
export function addIndicator(spec: StrategySpec, block: IndicatorBlock): StrategySpec {
  return { ...spec, indicators: [...spec.indicators, block] };
}

export function removeIndicatorAt(spec: StrategySpec, index: number): StrategySpec {
  return { ...spec, indicators: spec.indicators.filter((_, i) => i !== index) };
}

export function updateIndicatorAt(
  spec: StrategySpec,
  index: number,
  patch: Partial<IndicatorBlock>,
): StrategySpec {
  const current = spec.indicators[index];
  if (!current) return spec;
  const merged = { ...current, ...patch } as IndicatorBlock;
  const next = [...spec.indicators];
  next[index] = merged;
  return { ...spec, indicators: next };
}

// ---------------------------------------------------------------------------
// Entry block
// ---------------------------------------------------------------------------
export function addEntryCondition(
  spec: StrategySpec,
  cond: Condition | LogicNode,
): StrategySpec {
  const entry: EntryBlock = {
    ...spec.entry,
    conditions: [...spec.entry.conditions, cond],
  };
  return { ...spec, entry };
}

export function removeEntryConditionAt(spec: StrategySpec, index: number): StrategySpec {
  const entry: EntryBlock = {
    ...spec.entry,
    conditions: spec.entry.conditions.filter((_, i) => i !== index),
  };
  return { ...spec, entry };
}

export function updateEntryConditionAt(
  spec: StrategySpec,
  index: number,
  cond: Condition | LogicNode,
): StrategySpec {
  const conditions = [...spec.entry.conditions];
  conditions[index] = cond;
  return { ...spec, entry: { ...spec.entry, conditions } };
}

export function setEntryLogic(spec: StrategySpec, logic: 'AND' | 'OR'): StrategySpec {
  return { ...spec, entry: { ...spec.entry, logic } };
}

export function setEntrySize(
  spec: StrategySpec,
  size: EntryBlock['size'],
): StrategySpec {
  return { ...spec, entry: { ...spec.entry, size } };
}

// ---------------------------------------------------------------------------
// Exit block
// ---------------------------------------------------------------------------
export function addExitCondition(spec: StrategySpec, cond: Condition | LogicNode): StrategySpec {
  const exit: ExitBlock = {
    ...spec.exit,
    conditions: [...spec.exit.conditions, cond],
  };
  return { ...spec, exit };
}

export function removeExitConditionAt(spec: StrategySpec, index: number): StrategySpec {
  const exit: ExitBlock = {
    ...spec.exit,
    conditions: spec.exit.conditions.filter((_, i) => i !== index),
  };
  return { ...spec, exit };
}

export function updateExitConditionAt(
  spec: StrategySpec,
  index: number,
  cond: Condition | LogicNode,
): StrategySpec {
  const conditions = [...spec.exit.conditions];
  conditions[index] = cond;
  return { ...spec, exit: { ...spec.exit, conditions } };
}

export function setExitLogic(spec: StrategySpec, logic: 'AND' | 'OR'): StrategySpec {
  return { ...spec, exit: { ...spec.exit, logic } };
}

// ---------------------------------------------------------------------------
// Survival (Manage) block
// ---------------------------------------------------------------------------
export function addSurvivalRule(spec: StrategySpec, rule: SurvivalRule): StrategySpec {
  const survival = [...(spec.survival ?? []), rule];
  return { ...spec, survival };
}

export function removeSurvivalRuleAt(spec: StrategySpec, index: number): StrategySpec {
  const next = (spec.survival ?? []).filter((_, i) => i !== index);
  return { ...spec, survival: next };
}

export function updateSurvivalRuleAt(
  spec: StrategySpec,
  index: number,
  rule: SurvivalRule,
): StrategySpec {
  const next = [...(spec.survival ?? [])];
  next[index] = rule;
  return { ...spec, survival: next };
}

// ---------------------------------------------------------------------------
// Top-level
// ---------------------------------------------------------------------------
export function setStrategyName(spec: StrategySpec, name: string): StrategySpec {
  return { ...spec, name };
}

// ---------------------------------------------------------------------------
// Survival action constructors — small constructors for the survival rule
// editor so it doesn't have to know the typed-union shape.
// ---------------------------------------------------------------------------
export function buildCloseAllSurvivalRule(name: string, when: SurvivalRule['when']): SurvivalRule {
  const action: SurvivalAction = { kind: 'CLOSE_ALL' };
  return { name, when, action };
}
