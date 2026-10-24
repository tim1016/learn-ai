import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { formatReceiptLabel } from '../pipes/receipt-label.pipe';
import { formatTimestampDisplay } from '../timestamp/timestamp-display';
import type { DataSourceProvenance } from './data-source-provenance';
import { WorkspaceDataSourceComponent } from './workspace-data-source.component';

// 2026-06-19 16:00 America/New_York, and the session before it.
const WINDOW_END_MS = Date.UTC(2026, 5, 19, 20, 0, 0);
const WINDOW_START_MS = Date.UTC(2026, 5, 18, 13, 30, 0);
const OBSERVED_MS = Date.UTC(2026, 5, 19, 21, 5, 0);

const RECORDED: DataSourceProvenance = {
  sources: ['polygon', 'ibkr'],
  subject: 'SPY',
  requestedWindow: { startMs: WINDOW_START_MS - 86_400_000, endMs: WINDOW_END_MS },
  effectiveWindow: { startMs: WINDOW_START_MS, endMs: WINDOW_END_MS },
  warmup: '200 bars before the window',
  resolution: '1 minute',
  adjustment: 'split_adjusted',
  sessionPolicy: 'rth_only',
  dataIdentity: 'snap-Ab12Cd34',
  observedAtMs: OBSERVED_MS,
  counts: { added: 3, reused: 12, missing: 2 },
  limitations: ['Two minutes in the first session are missing.'],
};

/** A record whose server recorded only where the data came from and what was read. */
const SPARSE: DataSourceProvenance = {
  sources: ['lake'],
  subject: null,
  requestedWindow: null,
  effectiveWindow: { startMs: WINDOW_START_MS, endMs: WINDOW_END_MS },
  warmup: null,
  resolution: null,
  adjustment: null,
  sessionPolicy: null,
  dataIdentity: null,
  observedAtMs: null,
  counts: null,
  limitations: [],
};

/** The value beside a detail label. */
function valueOf(label: string): string {
  const row = screen.getByText(label).closest('div');
  if (row === null) throw new Error(`no row for ${label}`);
  return row.querySelector('dd')?.textContent?.replace(/\s+/g, ' ').trim() ?? '';
}

describe('WorkspaceDataSourceComponent', () => {
  it('reads "not recorded" for a legacy record that carries no provenance', async () => {
    await render(WorkspaceDataSourceComponent, { inputs: { provenance: null } });

    expect(screen.getByText('not recorded')).toBeTruthy();
    // Nothing is inferred to fill the gap, and there is nothing to expand.
    expect(screen.queryByText('Sources')).toBeNull();
  });

  it('projects the recorded provenance: sources, the window read, resolution, adjustment', async () => {
    await render(WorkspaceDataSourceComponent, { inputs: { provenance: RECORDED } });

    // Identifiers are worded by the shared label formatter, never shown raw.
    const sources = `${formatReceiptLabel('polygon')}, ${formatReceiptLabel('ibkr')}`;
    expect(screen.getByText(sources, { selector: 'summary span' })).toBeTruthy();
    expect(screen.getByText('1 minute', { selector: 'summary span' })).toBeTruthy();
    expect(screen.getByText(formatReceiptLabel('split_adjusted'), { selector: 'summary span' })).toBeTruthy();
    const start = formatTimestampDisplay(WINDOW_START_MS, { mode: 'et' });
    const end = formatTimestampDisplay(WINDOW_END_MS, { mode: 'et' });
    expect(valueOf('Effective window')).toBe(`${start} → ${end}`);
  });

  it('lists every recorded fact in the details, each worded by its owner', async () => {
    await render(WorkspaceDataSourceComponent, { inputs: { provenance: RECORDED } });

    expect(valueOf('Sources')).toBe(`${formatReceiptLabel('polygon')}, ${formatReceiptLabel('ibkr')}`);
    expect(valueOf('Symbol or universe')).toBe('SPY');
    expect(valueOf('Warmup')).toBe('200 bars before the window');
    expect(valueOf('Session policy')).toBe(formatReceiptLabel('rth_only'));
    // An opaque identity reaches the reader byte for byte.
    expect(valueOf('Data identity')).toBe('snap-Ab12Cd34');
    // The source data's own time, not the response's.
    expect(valueOf('Source data observed')).toBe(formatTimestampDisplay(OBSERVED_MS, { mode: 'local' }));
    expect(valueOf('This request')).toBe('3 added · 12 reused · 2 missing');
    expect(valueOf('Requested window')).not.toBe(valueOf('Effective window'));
    expect(screen.getByText('Two minutes in the first session are missing.')).toBeTruthy();
  });

  it('raises missing data and limitations on the collapsed line', async () => {
    await render(WorkspaceDataSourceComponent, { inputs: { provenance: RECORDED } });

    expect(screen.getByText('2 missing')).toBeTruthy();
    expect(screen.getByText('1 limitation')).toBeTruthy();
  });

  it('reads "not recorded" for each field the server did not record, and flags nothing it did not say', async () => {
    await render(WorkspaceDataSourceComponent, { inputs: { provenance: SPARSE } });

    for (const label of [
      'Symbol or universe',
      'Requested window',
      'Warmup',
      'Resolution',
      'Adjustment',
      'Session policy',
      'Data identity',
      'Source data observed',
      'This request',
    ]) {
      expect(valueOf(label), label).toBe('not recorded');
    }
    expect(valueOf('Sources')).toBe(formatReceiptLabel('lake'));
    // Unknown counts are not zero missing, and no limitations were stated.
    expect(screen.queryByText(/missing/)).toBeNull();
    expect(screen.queryByText(/limitation/)).toBeNull();
  });

  it('writes a date-anchored window as ET calendar dates', async () => {
    await render(WorkspaceDataSourceComponent, {
      inputs: { provenance: RECORDED, windowMode: 'date-et' },
    });

    const start = formatTimestampDisplay(WINDOW_START_MS, { mode: 'date-et' });
    const end = formatTimestampDisplay(WINDOW_END_MS, { mode: 'date-et' });
    expect(valueOf('Effective window')).toBe(`${start} → ${end}`);
  });
});
