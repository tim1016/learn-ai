/**
 * Contract-owned types used by the strategy-spec editor.
 *
 * Input shapes are aliases of the generated PythonDataService OpenAPI client.
 * The backtest result and mutation variables are aliases of the generated
 * GraphQL operation. This module only gives those generated contracts the
 * editor's stable, readable names; it does not mirror their fields.
 */

import type { components } from '../api/broker.types';
import type {
  RunSpecStrategyBacktestMutation,
  RunSpecStrategyBacktestMutationVariables,
} from './generated/graphql';

type Schema = components['schemas'];

export type IndicatorRef = Schema['IndicatorRef'];
export type ConstOperand = Schema['ConstOperand'];
export type SubtractOperand = Schema['Subtract-Input'];
export type Operand = Schema['IndicatorComparison-Input']['left'];

export type IndicatorKind = Schema['IndicatorBlock']['kind'];
export type BarSource = NonNullable<Schema['IndicatorBlock']['source']>;
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
export type PredictionComparisonCondition = Schema['PredictionComparison'];

export type LogicNode = Schema['LogicNode-Input'];
type ConditionOrLogic = Schema['EntryBlock-Input']['conditions'][number];
export type Condition = Exclude<ConditionOrLogic, LogicNode>;

export type SetHoldingsSize = Schema['SetHoldings'];
export type FixedContractsSize = Schema['FixedContracts'];
export type SizeRule = Schema['EntryBlock-Input']['size'];
export type EntryBlock = Schema['EntryBlock-Input'];
export type ExitBlock = Schema['ExitBlock-Input'];

export type CloseAllAction = Schema['CloseAllAction'];
export type SurvivalAction = Schema['SurvivalRule-Input']['action'];
export type SurvivalRule = Schema['SurvivalRule-Input'];

export type EquityLongPosition = Schema['EquityLongPosition'];
export type OptionTemplatePosition = Schema['OptionTemplatePosition'];
export type PositionSpec = NonNullable<Schema['StrategySpec-Input']['position']>;

export type Resolution = Schema['Resolution'];
export type Diagnostics = Schema['Diagnostics'];

/**
 * The editor always materializes the server-defaulted indicator list. This
 * local refinement still serializes to the generated API input contract.
 */
export type StrategySpec = Schema['StrategySpec-Input'] & {
  indicators: IndicatorBlock[];
};

export type RunSpecStrategyBacktestVariables = RunSpecStrategyBacktestMutationVariables;
export type RunSpecStrategyBacktestResponse = RunSpecStrategyBacktestMutation;
export type SpecStrategyBacktestResult = RunSpecStrategyBacktestMutation['runSpecStrategyBacktest'];
export type SpecStrategyTrade = SpecStrategyBacktestResult['trades'][number];
export type IndicatorSnapshotEntry = SpecStrategyTrade['indicators'][number];
