/**
 * What the server says about the data behind a result, for display.
 *
 * This is the display shape of server evidence (PRD #3490 R06), not a second
 * source of truth: the browser reads these facts and prints them. It derives
 * none of them: not the effective window from the requested one, not a count
 * from a window, not a source from the instrument.
 *
 * A field the server did not record is `null`, and the line reads "not
 * recorded" for it. It is never filled from today's holdings or a default.
 * A whole record with no provenance (a legacy record) is `null` at the
 * component, which reads "not recorded" for all of it.
 */

/** A span of time as the server states it. Both bounds are `int64 ms UTC`,
 * printed at the instants given: a host whose endpoint ends a window
 * exclusively converts to the end it wants shown before it builds this. */
export interface DataSourceWindow {
  readonly startMs: number;
  readonly endMs: number;
}

/** What this request did with the data it needed. Counts describe this
 * request, not every concurrent write to the same store. */
export interface DataSourceCounts {
  readonly added: number;
  readonly reused: number;
  readonly missing: number;
}

export interface DataSourceProvenance {
  /** Where the data came from. Vendor or store identifiers, shown through `receiptLabel`. */
  readonly sources: readonly string[];
  /** The symbol or universe the data is for. */
  readonly subject: string | null;
  readonly requestedWindow: DataSourceWindow | null;
  /** What was actually read; narrower than requested when data ran out. */
  readonly effectiveWindow: DataSourceWindow | null;
  /** Server-authored: the history read before the window for warmup. */
  readonly warmup: string | null;
  /** Server-authored bar resolution or aggregation, e.g. "1 minute". */
  readonly resolution: string | null;
  /** Adjustment mode identifier, shown through `receiptLabel`. */
  readonly adjustment: string | null;
  /** Session policy identifier, shown through `receiptLabel`. */
  readonly sessionPolicy: string | null;
  /** The data or snapshot identity, an opaque token printed exactly. */
  readonly dataIdentity: string | null;
  /** When the source data was observed: the data's time, not the response's. */
  readonly observedAtMs: number | null;
  readonly counts: DataSourceCounts | null;
  /** Server-authored caveats the reader should know. Empty: none stated. */
  readonly limitations: readonly string[];
}
