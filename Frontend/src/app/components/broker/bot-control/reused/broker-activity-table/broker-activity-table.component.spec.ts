import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';

import type { BrokerActivityHealth, OperatorNotice } from '../../../../../api/live-instances.types';
import type { ActivityBrokerCategorySummary } from '../bot-trade-chart-card/bot-trade-chart-card.types';
import { BrokerActivityTableComponent } from './broker-activity-table.component';
import type { BrokerActivityRow } from './broker-activity.types';

function row(overrides: Partial<BrokerActivityRow> = {}): BrokerActivityRow {
  return {
    seq: 1,
    ts_ms: 1_700_000_000_000,
    exec_id: 'exec-1',
    perm_id: 9001,
    order_ref: 'learn-ai/sid/v1/intent-1',
    symbol: 'SPY',
    side: 'BUY',
    quantity: 10,
    price: 420.5,
    commission: 1.0,
    net_amount: -4206.0,
    order_type: 'MKT',
    exec_ts_ms: 1_700_000_000_500,
    verdict: 'expected',
    template_key: 'normal_fill_v1',
    template_version: 1,
    headline: 'BUY 10 SPY @ $420.50',
    narrative: 'Filled as intended.',
    reason_codes: ['normal_fill'],
    engine_overlay: null,
    divergence_facts: null,
    ...overrides,
  };
}

function summary(overrides: Partial<ActivityBrokerCategorySummary> = {}): ActivityBrokerCategorySummary {
  return {
    category_id: 'evidence_broker_positions_refreshed',
    label: 'Broker positions refreshed',
    kind: 'heartbeat',
    event_count: 3,
    last_event_ts_ms: 1_700_000_000_000,
    row_ids: ['fold:evidence:reqPositionsAsync:position'],
    ...overrides,
  };
}

function healthNotice(
  code: OperatorNotice['code'],
  tier: OperatorNotice['tier'],
  title: string,
): OperatorNotice {
  const routed = code === 'activity.publisher_not_running';
  return {
    code,
    tier,
    title,
    message: `${title} message`,
    source_codes: [],
    forensic_facts: {},
    actionability: routed ? 'routed' : 'self_resolving',
    resolution: routed
      ? 'Clears when the data-plane publisher is restarted and running for this instance.'
      : 'Clears automatically when activity publisher evidence is healthy again.',
    remedy_status: null,
    action: routed
      ? {
          kind: 'external_manual_check',
          label: 'Check activity publisher',
          target: 'data_plane_activity_publisher',
        }
      : { kind: 'none', label: null, target: null },
    runbook_slug: 'broker-activity-health',
    occurred_at_ms: null,
  };
}

function health(
  state: BrokerActivityHealth['state'],
  headline: OperatorNotice | null = null,
): BrokerActivityHealth {
  return {
    state,
    headline,
    notices: headline ? [headline] : [],
    facts: {
      publisher_registered: state !== 'unavailable',
      publisher_running: state === 'ready' || state === 'degraded',
      latest_row_seq: null,
      seconds_since_registered: 10,
      seconds_since_last_row: null,
    },
  };
}

function renderFixture(props: {
  rows?: BrokerActivityRow[];
  eventSummary?: ActivityBrokerCategorySummary[];
  backfillLoading?: boolean;
  backfillError?: string | null;
  sseStatus?: string;
  sseError?: string | null;
  activityHealth?: BrokerActivityHealth | null;
}) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(BrokerActivityTableComponent);
  fixture.componentRef.setInput('rows', props.rows ?? []);
  fixture.componentRef.setInput('eventSummary', props.eventSummary ?? []);
  fixture.componentRef.setInput('backfillLoading', props.backfillLoading ?? false);
  fixture.componentRef.setInput('backfillError', props.backfillError ?? null);
  fixture.componentRef.setInput('sseStatus', props.sseStatus ?? 'open');
  fixture.componentRef.setInput('sseError', props.sseError ?? null);
  if (props.activityHealth !== undefined) {
    fixture.componentRef.setInput('activityHealth', props.activityHealth);
  }
  fixture.detectChanges();
  return fixture;
}

function render(props: Parameters<typeof renderFixture>[0]) {
  return renderFixture(props).nativeElement as HTMLElement;
}

afterEach(() => TestBed.resetTestingModule());

describe('BrokerActivityTableComponent', () => {
  it('renders broker-tail category cards without row drill-downs', () => {
    const el = render({
      eventSummary: [summary()],
      sseStatus: 'projection',
    });

    expect(el.querySelector('[aria-label="Broker tail projection"]')).not.toBeNull();
    expect(el.textContent ?? '').toContain('BROKER TAIL');
    expect(el.textContent ?? '').toContain('Broker positions refreshed');
    expect(el.textContent ?? '').toContain('3 events');
    expect(el.querySelector('table')).toBeNull();
    expect(el.querySelector('.event-drilldown')).toBeNull();
    expect(el.querySelector('.event-row-main')).toBeNull();
    expect(el.querySelector('.category-summary button')).toBeNull();
  });

  it('renders all broker-tail categories as static cards', () => {
    const el = render({
      eventSummary: [
        summary(),
        summary({
          category_id: 'order_fill',
          label: 'Broker fills',
          kind: 'order',
          row_ids: ['fill:exec-1'],
        }),
      ],
    });

    expect(el.textContent ?? '').toContain('Broker positions refreshed');
    expect(el.textContent ?? '').toContain('Broker fills');
    expect(el.querySelectorAll('.category-card')).toHaveLength(2);
  });

  it('updates the rendered card values from the latest backend summary input', () => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideZonelessChangeDetection()],
    });
    const fixture = TestBed.createComponent(BrokerActivityTableComponent);
    fixture.componentRef.setInput('rows', []);
    fixture.componentRef.setInput('eventSummary', [summary({ event_count: 1 })]);
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).textContent ?? '').toContain('1 event');

    fixture.componentRef.setInput('eventSummary', [summary({ event_count: 7 })]);
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).textContent ?? '').toContain('7 events');
  });

  it('does not render the legacy executed-fill table even when rows are supplied', () => {
    const el = render({ rows: [row({ symbol: 'AAPL' })] });
    expect(el.querySelector('table')).toBeNull();
    expect(el.textContent ?? '').not.toContain('AAPL');
  });

  it('shows loading and error states without revealing row details', () => {
    const loading = render({ backfillLoading: true });
    expect((loading.textContent ?? '').toLowerCase()).toContain('loading history');

    const error = render({ backfillError: 'boom' });
    const err = error.querySelector('[data-testid="broker-activity-backfill-error"]');
    expect(err).not.toBeNull();
    expect(err?.textContent ?? '').toContain('boom');
    expect(error.querySelector('table')).toBeNull();
  });

  it('renders health notice and suppresses summaries when unavailable or starting', () => {
    const notice = healthNotice('activity.publisher_not_running', 'critical', 'Activity feed is not running');
    const el = render({
      eventSummary: [summary()],
      activityHealth: health('unavailable', notice),
    });

    const healthEl = el.querySelector('[data-testid="broker-activity-health-notice"]');
    expect(healthEl).not.toBeNull();
    expect(healthEl?.textContent ?? '').toContain('Activity feed is not running');
    expect(el.textContent ?? '').not.toContain('Broker positions refreshed');
  });

  it('renders degraded health notice with summary cards', () => {
    const notice = healthNotice('activity.publisher_degraded', 'warning', 'Activity feed is degraded');
    const el = render({
      eventSummary: [summary()],
      activityHealth: health('degraded', notice),
    });

    expect(el.textContent ?? '').toContain('Activity feed is degraded');
    expect(el.textContent ?? '').toContain('Broker positions refreshed');
    expect(el.querySelector('table')).toBeNull();
  });
});
