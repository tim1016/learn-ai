import type { components } from '../../api/broker.types';

type Schemas = components['schemas'];

/** The setup as the routes accept it. A served setup (`SavedSetup.setup`) is assignable to it. */
export type SetupRequest = Schemas['OptionsBacktestSetup-Input'];
export type LegRequest = Schemas['LegSetup-Input'];
export type SessionTimeWire = Schemas['SessionTime'];
/** The setup as the list serves it: Decimals are strings. */
export type ServedSetup = Schemas['OptionsBacktestSetup-Output'];
export type ServedLeg = Schemas['LegSetup-Output'];
export type SavedSetup = Schemas['SavedSetupResponse'];
export type SetupSummary = Schemas['SetupDescriptionResponse'];
export type SessionClock = Schemas['SessionClockResponse'];
/** What the server holds a setup, a name and a range to (`GET /limits`). */
export type SetupLimits = Schemas['OptionsBacktestLimitsResponse'];

// Check data.
export type DataCheck = Schemas['OptionsBacktestDataCheckResponse'];
export type SessionCheck = Schemas['OptionsBacktestSessionCheckResponse'];
export type DataProblem = Schemas['OptionsBacktestProblemResponse'];
export type TestableStretch = Schemas['OptionsBacktestTestableStretchResponse'];

// A run: the history row, the opened run, and what a complete one holds.
export type RunListItem = Schemas['OptionsBacktestRunListItemResponse'];
export type RunDetail = Schemas['OptionsBacktestRunDetailResponse'];
export type RunStatus = RunListItem['status'];
export type RunFailure = Schemas['OptionsBacktestRunFailureResponse'];
export type RunBanner = Schemas['OptionsBacktestFillsBannerResponse'];
export type RunFigures = Schemas['OptionsBacktestRunFiguresResponse'];
export type Drawdown = Schemas['OptionsBacktestDrawdownResponse'];
export type DayRow = Schemas['OptionsBacktestDayRowResponse'];
export type MonthRow = Schemas['OptionsBacktestMonthRowResponse'];
export type SlippagePoint = Schemas['OptionsBacktestSlippagePointResponse'];
export type SlippageTolerance = Schemas['OptionsBacktestSlippageToleranceResponse'];
/** Whether the cash could have carried a run, and the run replayed at several starting cashes (#3108). */
export type CapitalEvidence = Schemas['OptionsBacktestCapitalEvidenceResponse'];
export type CapitalInvalidEvent = Schemas['OptionsBacktestCapitalInvalidEventResponse'];
export type CashLevel = Schemas['OptionsBacktestCashLevelResponse'];
export type RunStamps = Schemas['OptionsBacktestRunStampsResponse'];
/** How many entries were refused for one reason other than cash (#3109). A run saved before they were counted has none. */
export type EntryRefusalCount = Schemas['OptionsBacktestEntryRefusalCountResponse'];

/** The body of a run's job without its id: the jobs boundary adds `job_id`. */
export type RunStartRequest = Omit<Schemas['OptionsBacktestJobRequest'], 'job_id'>;

export type OptionRight = LegRequest['right'];
/** A leg with no side is bought, and a bought leg is served with none. */
export type LegSide = NonNullable<LegRequest['side']>;
export type SquareOff = SetupRequest['square_off'];
export type FeeKind = SetupRequest['fees']['kind'];
export type Anchor = SessionTimeWire['anchor'];
export type ExpiryKind = LegRequest['expiry']['kind'];
export type StrikeKind = LegRequest['strike']['kind'];
export type StopLossKind = NonNullable<LegRequest['stop_loss']>['kind'];
export type MomentumKind = NonNullable<LegRequest['momentum']>['kind'];
export type ReentryKind = NonNullable<LegRequest['reentry_on_stop']>['kind'];
