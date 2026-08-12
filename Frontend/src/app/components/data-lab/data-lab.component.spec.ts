import { signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { provideRouter } from '@angular/router';
import { render, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';
import { of } from 'rxjs';

import { DataLabSessionService } from '../../services/data-lab-session.service';
import { MarketMonitorService } from '../../services/market-monitor.service';
import { PastChainService } from '../../services/past-chain.service';
import { MarketDataService } from '../../services/market-data.service';
import { RunSessionService } from '../../services/run-session.service';
import { DataLabComponent } from './data-lab.component';

const savedSession = {
  id: 'session-1',
  name: 'Apple review',
  createdAt: '2026-05-15T12:00:00Z',
  updatedAt: '2026-05-15T12:00:00Z',
  ticker: 'AAPL',
  fromDate: '2026-04-15',
  toDate: '2026-05-15',
  indicatorCount: 2,
  hasChart: false,
};

function idleRunSession() {
  return {
    state: signal<'idle' | 'fetching' | 'bundling'>('idle'),
    fetchSummary: signal<{
      rawBars: number;
      processedBars: number;
      indicatorColumns: number;
    } | null>(null),
    start: vi.fn(async () => undefined),
    dockState: signal<'idle' | 'active' | 'done' | 'error'>('idle'),
    headline: signal('idle — no run in flight'),
    headlineLevel: signal<'info' | 'success' | 'warn' | 'error'>('info'),
    progressPercent: signal<number | null>(null),
    etaText: signal<string | null>(null),
    canCancel: signal(false),
    log: signal([]),
    clearLog: vi.fn(),
    cancel: vi.fn(async () => undefined),
  };
}

describe('DataLabComponent', () => {
  it('renders saved and active underlyings with the shared asset identity', async () => {
    const runSession = idleRunSession();
    const { container, fixture } = await render(DataLabComponent, {
      providers: [
        provideRouter([]),
        {
          provide: HttpClient,
          useValue: { get: () => of({ success: true, categories: {} }) },
        },
        {
          provide: DataLabSessionService,
          useValue: { listSessions: vi.fn(async () => [savedSession]) },
        },
        {
          provide: MarketMonitorService,
          useValue: { getHolidays: () => of([]) },
        },
        { provide: MarketDataService, useValue: {} },
        { provide: PastChainService, useValue: {} },
        { provide: RunSessionService, useValue: runSession },
      ],
    });

    await waitFor(() => {
      expect(fixture.componentInstance.savedSessions()).toEqual([savedSession]);
    });
    fixture.componentInstance.sessionPanelOpen.set(true);
    fixture.detectChanges();

    expect(
      container.querySelector('.gen-bar__context app-asset-identity')?.getAttribute('title'),
    ).toBe('SPY');
    expect(
      container.querySelector('.session-card-meta app-asset-identity')?.getAttribute('title'),
    ).toBe('AAPL');
    expect(container.querySelector('app-ticker-range-picker app-asset-identity')).toBeNull();

    fixture.componentInstance.ticker.set('AAPL');
    fixture.componentInstance.optionsCompanionEnabled.set(true);
    fixture.detectChanges();

    expect(
      container.querySelector('.cp-row .cp-asset-identity')?.getAttribute('title'),
    ).toBe('AAPL');
  });
});
