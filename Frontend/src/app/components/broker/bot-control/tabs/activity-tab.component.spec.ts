import { HttpClient } from '@angular/common/http';
import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { describe, expect, it } from 'vitest';

import type { LiveInstanceActivityProjection } from '../reused/bot-trade-chart-card/bot-trade-chart-card.types';
import { makeStatus } from '../bot-control-page.fixtures';
import {
  ActivityTabComponent,
  openOrderClustersForProjection,
} from './activity-tab.component';

function activityProjection(
  overrides: Partial<LiveInstanceActivityProjection> = {},
): LiveInstanceActivityProjection {
  return {
    schema_version: 1,
    strategy_instance_id: 'sid-a',
    session_date: '2026-06-29',
    timezone: 'America/New_York',
    symbol: 'SPY',
    resolution: '1m',
    has_bars: true,
    now_ms: 1_700_000_000_000,
    bars: [],
    fill_markers: [],
    position_annotations: [],
    order_overlays: [],
    orders_today: [],
    broker_activity_summary: [],
    broker_activity_rows: [],
    position_snapshot: [],
    reconciliation_warnings: [],
    evidence: [],
    ...overrides,
  };
}

describe('ActivityTabComponent', () => {
  it('resets linked chart selections when the bot identity changes', async () => {
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        { provide: HttpClient, useValue: { get: () => of(activityProjection()) } },
      ],
    });
    const fixture = TestBed.createComponent(ActivityTabComponent);
    fixture.componentRef.setInput('status', makeStatus({ id: 'sid-a' }));
    fixture.detectChanges();
    await fixture.whenStable();
    const component = fixture.componentInstance;
    component.selectedSessionDate.set('2026-06-01');
    component.selectedResolution.set('5s');

    fixture.componentRef.setInput('status', makeStatus({ id: 'sid-b' }));
    fixture.detectChanges();

    expect(component.selectedSessionDate()).not.toBe('2026-06-01');
    expect(component.selectedResolution()).toBe('1m');
  });
});

describe('openOrderClustersForProjection', () => {
  it('keeps only working/pending order clusters so resolved outcomes do not duplicate the stream tail', () => {
    const projection = activityProjection({
      orders_today: [
        {
          order_key: 'active',
          symbol: 'SPY',
          side: 'BUY',
          quantity: 1,
          order_type: 'MKT',
          status: 'submitted',
          group: 'active',
          chart_ts_ms: 1_700_000_000_000,
          submitted_ts_ms: 1_700_000_000_000,
          last_update_ts_ms: 1_700_000_001_000,
          filled_quantity: 0,
          avg_fill_price: null,
          position_effect: null,
          replay_count: 1,
          evidence: [],
        },
        {
          order_key: 'pending',
          symbol: 'SPY',
          side: 'SELL',
          quantity: 1,
          order_type: 'MKT',
          status: 'engine pending',
          group: 'engine_pending',
          chart_ts_ms: 1_700_000_002_000,
          submitted_ts_ms: 1_700_000_002_000,
          last_update_ts_ms: 1_700_000_002_000,
          filled_quantity: 0,
          avg_fill_price: null,
          position_effect: null,
          replay_count: 1,
          evidence: [],
        },
        {
          order_key: 'resolved',
          symbol: 'SPY',
          side: 'BUY',
          quantity: 1,
          order_type: 'MKT',
          status: 'filled',
          group: 'resolved',
          chart_ts_ms: 1_700_000_003_000,
          submitted_ts_ms: 1_700_000_003_000,
          last_update_ts_ms: 1_700_000_004_000,
          filled_quantity: 1,
          avg_fill_price: 420,
          position_effect: 'Open long',
          replay_count: 1,
          evidence: [],
        },
      ],
    });

    expect(openOrderClustersForProjection(projection).map((row) => row.order_key))
      .toEqual(['active', 'pending']);
    expect(openOrderClustersForProjection(null)).toEqual([]);
  });
});
