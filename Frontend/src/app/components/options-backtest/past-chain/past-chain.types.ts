import type { components } from '../../../api/broker.types';

type Schemas = components['schemas'];

/** The Past chain tab's wire shapes: aliases over the generated schemas only (ADR 0031). */

/** What the listing of one underlying held on one trading date. */
export type ChainDay = Schemas['PastChainDayResponse'];
export type ExpirySummary = Schemas['PastChainExpirySummary'];
export type ExpiryTag = Schemas['PastChainExpiryTag'];
export type TagSeries = ExpiryTag['series'];
/** Why the lake would fetch nothing for the session now, in its own words; absent or null when it would fetch. */
export type FetchRefusal = NonNullable<ChainDay['fetch_refusal']>;

/** One expiry at one instant of a session. */
export type ChainExpiry = Schemas['PastChainExpiryResponse'];
export type ChainSide = Schemas['PastChainSide'];
export type ChainContract = Schemas['PastChainContract'];
export type UnderlyingPrice = Schemas['PastChainUnderlyingPrice'];

/** A download: what to capture, and how each thing came out. */
export type DownloadRequest = Schemas['PastChainDownloadRequest'];
export type DownloadAnswer = Schemas['PastChainDownloadResponse'];
export type DownloadItem = Schemas['PastChainDownloadItem'];

/** The session of a trading date as an answer states it: the instants it opens and closes at. */
export interface SessionBounds {
  readonly openMs: number;
  readonly closeMs: number;
}
