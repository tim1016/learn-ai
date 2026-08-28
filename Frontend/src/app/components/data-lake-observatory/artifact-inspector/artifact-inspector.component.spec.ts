import { render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { DataLakeService } from '../lib/data-lake.service';
import type { ArtifactDetail, DataLakeRead } from '../lib/data-lake.types';
import { ArtifactInspectorComponent } from './artifact-inspector.component';

/** 09:30 America/New_York on 2026-05-20, as int64 ms UTC. */
const MAY_20_OPEN_MS = Date.UTC(2026, 4, 20, 13, 30);
const FETCHED_AT_MS = Date.UTC(2026, 4, 20, 21, 5, 30);

function fakeDetail(overrides: Partial<ArtifactDetail> = {}): ArtifactDetail {
  return {
    id: 42,
    artifact_kind: 'minute_trade',
    market: 'usa',
    symbol: 'SPY',
    trading_date_ms: MAY_20_OPEN_MS,
    resolution: 'minute',
    data_type: 'trade',
    provider: 'polygon',
    provider_params: { adjusted: false, lean_image_digest: 'sha256:pinned' },
    price_adjustment_mode: 'raw',
    data_contract_hash: 'dch-aaaabbbbccccdddd',
    content_hash: 'sha256-eeeeffff00001111',
    file_path: '/lake/usa/minute/spy/20260520_trade.zip',
    file_size_bytes: 2_097_152,
    status: 'complete',
    row_count: 390,
    first_bar_start_ms: MAY_20_OPEN_MS,
    last_bar_start_ms: MAY_20_OPEN_MS + 23_340_000,
    fetched_at_ms: FETCHED_AT_MS,
    completed_at_ms: FETCHED_AT_MS + 4_000,
    attempt_count: 1,
    last_error: null,
    error_message: null,
    ...overrides,
  };
}

async function renderInspector(read: DataLakeRead<ArtifactDetail>, artifactId = 42) {
  const artifact = vi.fn().mockResolvedValue(read);
  const view = await render(ArtifactInspectorComponent, {
    providers: [{ provide: DataLakeService, useValue: { artifact } }],
    componentInputs: { artifactId, symbol: 'SPY' },
  });
  return { ...view, artifact };
}

/** The rendered value of one receipt row, found by its visible label. */
function receiptValue(container: Element, label: string): string {
  const term = Array.from(container.querySelectorAll('.receipt__label')).find(
    (node) => node.textContent?.trim() === label,
  );
  if (term === undefined) throw new Error(`no receipt row labelled ${label}`);
  return term.nextElementSibling?.textContent?.trim() ?? '';
}

describe('ArtifactInspectorComponent', () => {
  it('shows the hashes and the path verbatim', async () => {
    await renderInspector({ kind: 'ok', value: fakeDetail() });

    expect(await screen.findByText('dch-aaaabbbbccccdddd')).toBeTruthy();
    expect(screen.getByText('sha256-eeeeffff00001111')).toBeTruthy();
    expect(screen.getByText('/lake/usa/minute/spy/20260520_trade.zip')).toBeTruthy();
  });

  it('names the symbol and market exactly as the catalog holds them', async () => {
    // The receipt pipe title-cases a code-like value, which would print
    // "Spy" and "Usa" here while the heatmap row header and the panel's own
    // eyebrow print "SPY". A receipt that disagrees with the grid above it
    // about which symbol it describes is worse than no receipt.
    const { container } = await renderInspector({ kind: 'ok', value: fakeDetail() });
    await screen.findByText('dch-aaaabbbbccccdddd');

    expect(receiptValue(container, 'Symbol')).toBe('SPY');
    expect(receiptValue(container, 'Market')).toBe('usa');
    expect(screen.queryByText('Spy')).toBeNull();
    expect(screen.queryByText('Usa')).toBeNull();
  });

  it('reports size in both human and exact byte terms', async () => {
    await renderInspector({ kind: 'ok', value: fakeDetail() });

    expect(await screen.findByText('2.00 MB (2097152 bytes)')).toBeTruthy();
  });

  it('renders the provider parameters that produced the artifact', async () => {
    await renderInspector({ kind: 'ok', value: fakeDetail() });

    expect(await screen.findByText('sha256:pinned')).toBeTruthy();
    expect(screen.getByText('Adjusted')).toBeTruthy();
  });

  it('renders the trading date in ET, never shifted to the viewer local day', async () => {
    await renderInspector({ kind: 'ok', value: fakeDetail() });

    const tradingDate = await screen.findByText('2026-05-20');
    expect(tradingDate.getAttribute('data-timestamp-mode')).toBe('date-et');
  });

  it('renders instants in the viewer zone, not ET', async () => {
    const { container } = await renderInspector({ kind: 'ok', value: fakeDetail() });
    await screen.findByText('2026-05-20');

    const modes = Array.from(container.querySelectorAll('[data-timestamp-mode]')).map((node) =>
      node.getAttribute('data-timestamp-mode'),
    );
    expect(modes).toContain('local');
  });

  it('says a content hash is absent rather than showing a blank one', async () => {
    await renderInspector({ kind: 'ok', value: fakeDetail({ content_hash: null, status: 'fetching' }) });

    expect(
      await screen.findByText('Not recorded until the artifact reaches complete.'),
    ).toBeTruthy();
  });

  it('names a failed row diagnosis through the receipt-label pipe', async () => {
    await renderInspector({
      kind: 'ok',
      value: fakeDetail({
        status: 'failed',
        last_error: 'provider_rate_limited',
        error_message: 'Polygon returned 429 four times.',
        attempt_count: 4,
      }),
    });

    expect(await screen.findByText('Provider Rate Limited')).toBeTruthy();
    expect(screen.getByText('Polygon returned 429 four times.')).toBeTruthy();
  });

  it('says so honestly when the catalog has no such row', async () => {
    await renderInspector({ kind: 'not_enabled' }, 99);

    expect(await screen.findByText(/No receipt for artifact 99/)).toBeTruthy();
  });

  it('surfaces a rejection reason instead of a blank panel', async () => {
    await renderInspector({ kind: 'unavailable', message: 'The data plane did not respond.' });

    expect(await screen.findByText('Unavailable')).toBeTruthy();
    expect(screen.getByText('The data plane did not respond.')).toBeTruthy();
  });

  it('passes AXE', async () => {
    await renderInspector({ kind: 'ok', value: fakeDetail() });
    await screen.findByText('dch-aaaabbbbccccdddd');

    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false }, region: { enabled: false } },
    });

    expect(results.violations).toEqual([]);
  });
});
