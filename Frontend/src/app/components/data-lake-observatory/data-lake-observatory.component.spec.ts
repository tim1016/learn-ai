import { fireEvent, render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { JobsService } from '../../services/jobs.service';
import { DataLakeObservatoryComponent } from './data-lake-observatory.component';
import { DataLakeService } from './lib/data-lake.service';
import type {
  ArtifactDetail,
  BackfillDefaults,
  CoverageResponse,
  CoverageStatus,
  DataLakeRead,
  StorageSummaryResponse,
} from './lib/data-lake.types';

/** 09:30 America/New_York on the given May 2026 date, as int64 ms UTC. */
function sessionOpenMs(day: number): number {
  return Date.UTC(2026, 4, day, 13, 30);
}

const DEFAULTS: BackfillDefaults = {
  market: 'usa',
  lean_image_digest: 'sha256:pinned',
  max_trading_range_days: 1830,
  max_symbol_length: 20,
};

const EMPTY_STORAGE: StorageSummaryResponse = { market: 'usa', kinds: [], symbols: [] };

const POPULATED_STORAGE: StorageSummaryResponse = {
  market: 'usa',
  kinds: [
    { artifact_kind: 'minute_trade', resolution: 'minute', artifact_count: 2, total_bytes: 4096 },
  ],
  symbols: [
    {
      symbol: 'SPY',
      first_trading_date_ms: sessionOpenMs(18),
      last_trading_date_ms: sessionOpenMs(20),
      artifact_count: 2,
    },
  ],
};

function coverage(
  days: readonly { day: number; status: CoverageStatus; artifactId?: number }[],
): DataLakeRead<CoverageResponse> {
  return {
    kind: 'ok',
    value: {
      market: 'usa',
      symbol: 'SPY',
      data_type: 'trade',
      resolution: 'minute',
      provider: 'polygon',
      price_adjustment_mode: 'raw',
      days: days.map((entry) => ({
        trading_date_ms: sessionOpenMs(entry.day),
        status: entry.status,
        artifact_id: entry.artifactId ?? null,
      })),
    },
  };
}

const ARTIFACT: ArtifactDetail = {
  id: 11,
  artifact_kind: 'minute_trade',
  market: 'usa',
  symbol: 'SPY',
  trading_date_ms: sessionOpenMs(18),
  resolution: 'minute',
  data_type: 'trade',
  provider: 'polygon',
  provider_params: { adjusted: false },
  price_adjustment_mode: 'raw',
  data_contract_hash: 'dch-1234',
  content_hash: 'sha256-abcd',
  file_path: '/lake/usa/minute/spy/20260518_trade.zip',
  file_size_bytes: 1024,
  status: 'complete',
  row_count: 390,
  first_bar_start_ms: sessionOpenMs(18),
  last_bar_start_ms: sessionOpenMs(18),
  fetched_at_ms: sessionOpenMs(18) + 1_000,
  completed_at_ms: sessionOpenMs(18) + 2_000,
  attempt_count: 1,
  last_error: null,
  error_message: null,
};

let originalEventSource: typeof EventSource;

interface LakeStubs {
  storage?: DataLakeRead<StorageSummaryResponse>;
  defaults?: DataLakeRead<BackfillDefaults>;
  coverage?: DataLakeRead<CoverageResponse>;
  artifact?: DataLakeRead<ArtifactDetail>;
}

async function renderObservatory(stubs: LakeStubs = {}) {
  const lake = {
    storageSummary: vi.fn().mockResolvedValue(stubs.storage ?? { kind: 'ok', value: EMPTY_STORAGE }),
    backfillDefaults: vi.fn().mockResolvedValue(stubs.defaults ?? { kind: 'ok', value: DEFAULTS }),
    coverage: vi
      .fn()
      .mockResolvedValue(stubs.coverage ?? coverage([{ day: 18, status: 'complete', artifactId: 11 }])),
    artifact: vi.fn().mockResolvedValue(stubs.artifact ?? { kind: 'ok', value: ARTIFACT }),
  };
  const view = await render(DataLakeObservatoryComponent, {
    providers: [
      { provide: DataLakeService, useValue: lake },
      { provide: JobsService, useValue: { startJob: vi.fn(), cancelJob: vi.fn() } },
    ],
  });
  return { ...view, lake };
}

/** Fill the symbol box and press Load coverage. */
async function loadSymbols(symbols = 'SPY'): Promise<void> {
  fireEvent.input(screen.getByLabelText('Symbols'), { target: { value: symbols } });
  fireEvent.click(screen.getByRole('button', { name: 'Load coverage' }));
}

describe('DataLakeObservatoryComponent', () => {
  beforeEach(() => {
    originalEventSource = globalThis.EventSource;
    class StubEventSource {
      onmessage: ((event: MessageEvent<string>) => void) | null = null;
      onerror: (() => void) | null = null;
      close(): void {}
    }
    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
      StubEventSource as unknown as typeof EventSource;
  });

  afterEach(() => {
    (globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
      originalEventSource;
  });

  it('names the dark lake instead of spinning or crashing', async () => {
    await renderObservatory({ storage: { kind: 'not_enabled' }, defaults: { kind: 'not_enabled' } });

    expect(await screen.findByText(/data lake is not enabled/)).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Coverage' })).toBeNull();
  });

  it('renders an empty catalog honestly rather than as zeroed tables', async () => {
    await renderObservatory();

    expect(await screen.findByText(/The catalog holds no artifacts yet/)).toBeTruthy();
  });

  it('asks for a symbol before claiming anything about coverage', async () => {
    await renderObservatory();

    expect(
      await screen.findByText('Name a symbol above to see which sessions are on disk.'),
    ).toBeTruthy();
  });

  it('reads coverage for each symbol the operator names', async () => {
    const { lake } = await renderObservatory();
    await screen.findByText(/The catalog holds no artifacts yet/);

    await loadSymbols('spy, aapl');

    await vi.waitFor(() => expect(lake.coverage).toHaveBeenCalledTimes(2));
    expect(lake.coverage.mock.calls.map(([query]) => (query as { symbol: string }).symbol)).toEqual([
      'SPY',
      'AAPL',
    ]);
  });

  it('opens the inspector on a clicked cell', async () => {
    const { lake } = await renderObservatory({ storage: { kind: 'ok', value: POPULATED_STORAGE } });
    await loadSymbols();

    const cell = await screen.findByRole('button', {
      name: 'SPY, 2026-05-18, Complete, open artifact receipt',
    });
    fireEvent.click(cell);

    await vi.waitFor(() => expect(lake.artifact).toHaveBeenCalledWith(11));
    expect(await screen.findByText('dch-1234')).toBeTruthy();
    expect(screen.getByText('/lake/usa/minute/spy/20260518_trade.zip')).toBeTruthy();
  });

  it('surfaces a rejected window under its own reason code', async () => {
    await renderObservatory({
      coverage: { kind: 'rejected', reason: 'range_too_large', message: 'range is 3654 days' },
    });
    await loadSymbols();

    expect(await screen.findByText('SPY · Range Too Large')).toBeTruthy();
    expect(screen.getByText('range is 3654 days')).toBeTruthy();
  });

  it('passes AXE with a populated catalog', async () => {
    await renderObservatory({ storage: { kind: 'ok', value: POPULATED_STORAGE } });
    await loadSymbols();
    await screen.findByRole('button', {
      name: 'SPY, 2026-05-18, Complete, open artifact receipt',
    });

    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false }, region: { enabled: false } },
    });

    expect(results.violations).toEqual([]);
  });
});
