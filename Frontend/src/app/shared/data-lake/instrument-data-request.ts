/** The lake trees a capture can write, and so the trees a reading can be asked of. */
export type LakeTree = 'raw' | 'polygon_split_adjusted';

/**
 * What Python is asked about (`GET /api/data-lake/instrument-data`): one instrument's whole-day minute bars over a
 * window of dates, in one tree. The Data tab asks it of the committed window and a lake study's page of the window its
 * run is given; Python names the sessions in it, and both then capture the span it names with the same backfill job.
 */
export interface InstrumentDataRequest {
  readonly symbol: string;
  /** `YYYY-MM-DD`, both included: converted to `int64 ms UTC` at the HTTP seam, as `coverage` does. */
  readonly from: string;
  readonly to: string;
  /** No default, for the reason `CoverageQuery.priceAdjustmentMode` gives. */
  readonly tree: LakeTree;
}

export function sameInstrumentDataRequest(a: InstrumentDataRequest | null, b: InstrumentDataRequest | null): boolean {
  if (a === null || b === null) return a === b;
  return a.symbol === b.symbol && a.from === b.from && a.to === b.to && a.tree === b.tree;
}
