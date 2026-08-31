import type { CoverageResponse, CoverageStatus, DataLakeRead } from './data-lake.types';

export interface CoverageCell {
  /** Date-anchored session open, int64 ms UTC. Render in `date-et`. */
  readonly tradingDateMs: number;
  readonly status: CoverageStatus;
  readonly artifactId: number | null;
}

export interface CoverageRow {
  readonly symbol: string;
  readonly cells: readonly CoverageCell[];
  readonly counts: Readonly<Record<CoverageStatus, number>>;
}

/** A symbol whose coverage request did not return a board row, and why. */
export interface CoverageProblem {
  readonly symbol: string;
  /** Backend reason code, or `unavailable` for a transport failure. Render through `receiptLabel`. */
  readonly reason: string;
  readonly message: string;
}

export interface CoverageBoard {
  readonly rows: readonly CoverageRow[];
  readonly problems: readonly CoverageProblem[];
  readonly sessionCount: number;
  readonly firstSessionMs: number | null;
  readonly lastSessionMs: number | null;
}

/**
 * The four artifact states plus the synthesized `missing`, in the order the
 * legend and the per-row tallies present them. Ordering is deliberate:
 * healthy first, then in-flight, then the two that need an operator.
 */
export const COVERAGE_STATUSES: readonly CoverageStatus[] = [
  'complete',
  'fetching',
  'stale',
  'failed',
  'missing',
];

/**
 * Glyph per state, so the heatmap is readable without colour (WCAG 1.4.1).
 * Each cell carries its glyph *and* its state in the accessible name; colour
 * is the third, redundant channel.
 */
const STATUS_GLYPHS: Readonly<Record<CoverageStatus, string>> = {
  complete: '✓',
  fetching: '◐',
  stale: '~',
  failed: '✕',
  missing: '·',
};

export function coverageGlyph(status: CoverageStatus): string {
  return STATUS_GLYPHS[status];
}

function emptyCounts(): Record<CoverageStatus, number> {
  return { complete: 0, fetching: 0, stale: 0, failed: 0, missing: 0 };
}

function toRow(response: CoverageResponse): CoverageRow {
  const counts = emptyCounts();
  const cells = response.days.map((day) => {
    counts[day.status] += 1;
    return {
      tradingDateMs: day.trading_date_ms,
      status: day.status,
      artifactId: day.artifact_id ?? null,
    };
  });
  return { symbol: response.symbol, cells, counts };
}

/**
 * Folds one coverage read per symbol into the board the heatmap renders.
 *
 * The endpoint walks the canonical NYSE calendar, so a weekend or holiday is
 * simply absent from `days` — never a cell, never a gap to explain. The
 * session axis is therefore whatever the calendar returned; it is read off
 * the longest row so a symbol whose request failed cannot shorten it.
 */
export function buildCoverageBoard(
  reads: readonly { readonly symbol: string; readonly read: DataLakeRead<CoverageResponse> }[],
): CoverageBoard {
  const rows: CoverageRow[] = [];
  const problems: CoverageProblem[] = [];

  for (const { symbol, read } of reads) {
    switch (read.kind) {
      case 'ok':
        rows.push(toRow(read.value));
        break;
      case 'rejected':
        problems.push({ symbol, reason: read.reason, message: read.message });
        break;
      case 'unavailable':
        problems.push({ symbol, reason: 'unavailable', message: read.message });
        break;
    }
  }

  const axis = rows.reduce<readonly CoverageCell[]>(
    (longest, row) => (row.cells.length > longest.length ? row.cells : longest),
    [],
  );
  return {
    rows,
    problems,
    sessionCount: axis.length,
    firstSessionMs: axis.length > 0 ? axis[0].tradingDateMs : null,
    lastSessionMs: axis.length > 0 ? axis[axis.length - 1].tradingDateMs : null,
  };
}

/**
 * Mirrors `SYMBOL_RE` in `PythonDataService/app/data_lake/types.py`.
 *
 * Exported so `coverage-board.spec.ts` can pin its source against the
 * backend pattern character for character. A duplicated grammar that
 * drifted would start rejecting symbols the catalog accepts, or waving
 * through ones it cannot store — the parity test makes that a failure
 * rather than a silent divergence.
 */
export const SYMBOL_PATTERN = /^[A-Z][A-Z0-9.]*$/;

export interface ParsedSymbols {
  readonly symbols: readonly string[];
  readonly invalid: readonly string[];
}

/**
 * Splits an operator's comma- or space-separated symbol list.
 *
 * Upper-cases and de-duplicates, then partitions against the catalog's own
 * symbol grammar and length cap so an unusable entry is named in the form
 * rather than spent on a 422 round trip.
 */
export function parseSymbols(raw: string, maxLength: number): ParsedSymbols {
  const seen = new Set<string>();
  const symbols: string[] = [];
  const invalid: string[] = [];
  for (const token of raw.split(/[\s,]+/)) {
    if (token === '') continue;
    const candidate = token.toUpperCase();
    if (seen.has(candidate)) continue;
    seen.add(candidate);
    if (SYMBOL_PATTERN.test(candidate) && candidate.length <= maxLength) {
      symbols.push(candidate);
    } else {
      invalid.push(token);
    }
  }
  return { symbols, invalid };
}
