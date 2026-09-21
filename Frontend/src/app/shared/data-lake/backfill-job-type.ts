/**
 * The public job type the .NET jobs framework forwards to the data-lake
 * router. Lives beside the lake contract (not in the Observatory store) so
 * any submitter of a backfill job — the operator panel and the picker's
 * ensure-coverage gate alike — names it from one place.
 */
export const BACKFILL_JOB_TYPE = 'data_lake_backfill';
