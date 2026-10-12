import type { components } from '../../../api/broker.types';

type Schemas = components['schemas'];

/** The Past chain tab's wire shapes: aliases over the generated schemas only (ADR 0031). */

/** What the listing of one underlying held on one trading date. */
export type ChainDay = Schemas['PastChainDayResponse'];
export type ExpirySummary = Schemas['PastChainExpirySummary'];
export type ExpiryTag = Schemas['PastChainExpiryTag'];
export type TagSeries = ExpiryTag['series'];
export type ListingState = ChainDay['listing_state'];

/** One expiry at one instant of a session. */
export type ChainExpiry = Schemas['PastChainExpiryResponse'];
export type ChainSide = Schemas['PastChainSide'];
export type ChainContract = Schemas['PastChainContract'];
export type PriceState = ChainContract['price_state'];
export type UnderlyingPrice = Schemas['PastChainUnderlyingPrice'];
export type UnderlyingState = UnderlyingPrice['state'];

/** A download: what to capture, and how each thing came out. */
export type DownloadRequest = Schemas['PastChainDownloadRequest'];
export type DownloadAnswer = Schemas['PastChainDownloadResponse'];
export type DownloadItem = Schemas['PastChainDownloadItem'];
export type DownloadReason = NonNullable<DownloadItem['reason']>;

/** The session of a trading date as an answer states it: the instants it opens and closes at. */
export interface SessionBounds {
  readonly openMs: number;
  readonly closeMs: number;
}
