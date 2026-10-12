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
export type Refusal = Schemas['OptionsBacktestRefusal'];

export type OptionRight = LegRequest['right'];
export type SquareOff = SetupRequest['square_off'];
export type FeeKind = SetupRequest['fees']['kind'];
export type Anchor = SessionTimeWire['anchor'];
export type ExpiryKind = LegRequest['expiry']['kind'];
export type StrikeKind = LegRequest['strike']['kind'];
export type StopLossKind = NonNullable<LegRequest['stop_loss']>['kind'];
export type MomentumKind = NonNullable<LegRequest['momentum']>['kind'];
export type ReentryKind = NonNullable<LegRequest['reentry_on_stop']>['kind'];
