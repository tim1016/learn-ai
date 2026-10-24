import { describe, expect, it } from 'vitest';

import { artifactReceiptSections, formatBytes, type ReceiptRow } from './artifact-receipt';
import type { ArtifactDetail } from '../../../shared/data-lake';

/** 09:30 America/New_York on 2026-05-20, as int64 ms UTC. */
const MAY_20_OPEN_MS = Date.UTC(2026, 4, 20, 13, 30);

function fakeDetail(overrides: Partial<ArtifactDetail> = {}): ArtifactDetail {
  return {
    id: 7,
    artifact_kind: 'minute_trade',
    market: 'usa',
    symbol: 'SPY',
    trading_date_ms: MAY_20_OPEN_MS,
    resolution: 'minute',
    data_type: 'trade',
    provider: 'polygon',
    provider_params: {},
    price_adjustment_mode: 'raw',
    data_contract_hash: 'dch-1',
    content_hash: 'sha256-1',
    file_path: '/lake/a.zip',
    file_size_bytes: 1024,
    status: 'complete',
    row_count: 390,
    first_bar_start_ms: MAY_20_OPEN_MS,
    last_bar_start_ms: MAY_20_OPEN_MS,
    fetched_at_ms: MAY_20_OPEN_MS + 1_000,
    completed_at_ms: MAY_20_OPEN_MS + 2_000,
    attempt_count: 1,
    last_error: null,
    error_message: null,
    ...overrides,
  };
}

function rowFor(detail: ArtifactDetail, label: string): ReceiptRow | undefined {
  return artifactReceiptSections(detail)
    .flatMap((section) => section.rows)
    .find((row) => row.label === label);
}

describe('formatBytes', () => {
  it.each([
    [0, '0 B'],
    [512, '512 B'],
    [1024, '1.00 KB'],
    [2_097_152, '2.00 MB'],
    [1_099_511_627_776, '1.00 TB'],
    // A non-finite value gets no invented size.
    [Number.NaN, '—'],
  ])('renders %i bytes as %s', (bytes, expected) => {
    expect(formatBytes(bytes)).toBe(expected);
  });
});

describe('artifactReceiptSections', () => {
  it('classifies every row of the default artifact receipt and drops the section that would render empty', () => {
    const detail = fakeDetail();

    expect
      .soft(rowFor(detail, 'trading_date')?.kind, 'marks a trading date as date-anchored')
      .toBe('date');
    expect
      .soft(rowFor(detail, 'fetched_at')?.kind, 'marks a fetch time as an instant')
      .toBe('instant');

    for (const label of ['data_contract_hash', 'content_hash', 'file_path', 'artifact_id']) {
      expect
        .soft(rowFor(detail, label)?.kind, `keeps ${label} as a verbatim audit token`)
        .toBe('exact');
    }

    expect
      .soft(rowFor(detail, 'artifact_kind')?.kind, 'routes artifact_kind through the code channel, not verbatim')
      .toBe('code');
    expect
      .soft(rowFor(detail, 'status')?.kind, 'routes status through the code channel, not verbatim')
      .toBe('code');

    const titles = artifactReceiptSections(detail).map((section) => section.title);
    expect.soft(titles, 'drops a section that would otherwise render empty').not.toContain('Provider parameters');
  });

  it('reproduces the catalog symbol and market exactly, never title-cased', () => {
    // The receipt pipe would render "AAPL" as "Aapl" and "usa" as "Usa" —
    // neither is what the catalog holds, and both disagree with the heatmap
    // row header and the storage-summary table naming the same values.
    const detail = fakeDetail({ symbol: 'AAPL', market: 'usa' });

    expect(rowFor(detail, 'symbol')).toEqual({ kind: 'exact', label: 'symbol', value: 'AAPL' });
    expect(rowFor(detail, 'market')).toEqual({ kind: 'exact', label: 'market', value: 'usa' });
  });

  it('says an absent content hash is absent instead of showing an empty token', () => {
    const detail = fakeDetail({ content_hash: null, status: 'fetching' });

    expect(rowFor(detail, 'content_hash')).toMatchObject({
      kind: 'prose',
      value: 'Not recorded until the artifact reaches complete.',
    });
  });

  it('applies the shared opaque-label rule to provider parameters', () => {
    const detail = fakeDetail({
      provider_params: { adjusted: false, lean_image_digest: 'sha256:x', window: { limit: 5 } },
    });

    expect(rowFor(detail, 'adjusted')?.kind).toBe('code');
    expect(rowFor(detail, 'lean_image_digest')?.kind).toBe('exact');
    expect(rowFor(detail, 'window')).toMatchObject({ kind: 'exact', value: '{"limit":5}' });
  });
});
