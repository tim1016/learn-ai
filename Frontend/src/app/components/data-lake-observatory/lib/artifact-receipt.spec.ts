import { describe, expect, it } from 'vitest';

import { artifactReceiptSections, formatBytes, type ReceiptRow } from './artifact-receipt';
import type { ArtifactDetail } from './data-lake.types';

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
  ])('renders %i bytes as %s', (bytes, expected) => {
    expect(formatBytes(bytes)).toBe(expected);
  });

  it('refuses to invent a size for a non-finite value', () => {
    expect(formatBytes(Number.NaN)).toBe('—');
  });
});

describe('artifactReceiptSections', () => {
  it('marks a trading date as date-anchored and a fetch time as an instant', () => {
    const detail = fakeDetail();

    expect(rowFor(detail, 'trading_date')?.kind).toBe('date');
    expect(rowFor(detail, 'fetched_at')?.kind).toBe('instant');
  });

  it('keeps hashes and the file path as verbatim audit tokens', () => {
    const detail = fakeDetail();

    for (const label of ['data_contract_hash', 'content_hash', 'file_path', 'artifact_id']) {
      expect(rowFor(detail, label)?.kind).toBe('exact');
    }
  });

  it('routes backend identifiers through the code channel, not verbatim', () => {
    const detail = fakeDetail();

    expect(rowFor(detail, 'artifact_kind')?.kind).toBe('code');
    expect(rowFor(detail, 'status')?.kind).toBe('code');
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

  it('drops a section that would otherwise render empty', () => {
    const titles = artifactReceiptSections(fakeDetail()).map((section) => section.title);

    expect(titles).not.toContain('Provider parameters');
  });
});
