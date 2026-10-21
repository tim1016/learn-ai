/**
 * Contract-owned types used by the strategy-spec editor.
 *
 * Every alias is a generated PythonDataService OpenAPI type: the spec input
 * shapes and, since #1963, the request and response the runner exchanges with
 * `POST /api/spec-strategy/backtest` directly. This module only gives those
 * generated contracts the editor's stable, readable names; it does not mirror
 * their fields.
 */

import type { components } from '../api/broker.types';

type Schema = components['schemas'];

export type Operand = Schema['IndicatorComparison-Input']['left'];

export type IndicatorBlock = Schema['IndicatorBlock'];

export type ComparisonOp = Schema['IndicatorComparison-Input']['op'];
export type IndicatorComparisonCondition = Schema['IndicatorComparison-Input'];
export type IndicatorBetweenCondition = Schema['IndicatorBetween'];
export type FreshCrossCondition = Schema['FreshCross'];
export type BarsSinceEntryCondition = Schema['BarsSinceEntry'];
export type TimeOfDayCondition = Schema['TimeOfDay'];
export type PnLPercentCondition = Schema['PnLPercent'];
export type PnLPointsCondition = Schema['PnLPoints'];
export type DrawdownFromPeakCondition = Schema['DrawdownFromPeak'];
export type BarPropertyCondition = Schema['BarProperty'];

export type LogicNode = Schema['LogicNode-Input'];
type ConditionOrLogic = Schema['EntryBlock-Input']['conditions'][number];
export type Condition = Exclude<ConditionOrLogic, LogicNode>;

export type EntryBlock = Schema['EntryBlock-Input'];
export type ExitBlock = Schema['ExitBlock-Input'];

export type SurvivalAction = Schema['SurvivalRule-Input']['action'];
export type SurvivalRule = Schema['SurvivalRule-Input'];

/**
 * The editor always materializes the server-defaulted indicator list. This
 * local refinement still serializes to the generated API input contract.
 */
export type StrategySpec = Schema['StrategySpec-Input'] & {
  indicators: IndicatorBlock[];
};

/** Wire shapes of `POST /api/spec-strategy/backtest` — snake_case, exactly as Python emits them. */
export type SpecBacktestRequest = Schema['SpecBacktestRequest'];
export type SpecStrategyBacktestResult = Schema['SpecBacktestResponse'];
export type SpecStrategyTrade = Schema['SpecTradeResponse'];
