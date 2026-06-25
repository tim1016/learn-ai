import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';

import type { BrokerActivityHealth, OperatorNotice } from '../../../../../api/live-instances.types';
import { BrokerActivityTableComponent } from './broker-activity-table.component';
import type { ActivityBrokerEventRow } from '../bot-trade-chart-card/bot-trade-chart-card.types';
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

function healthNotice(
  code: OperatorNotice['code'],
  tier: OperatorNotice['tier'],
  title: string,
): OperatorNotice {
  return {
    code,
    tier,
    title,
    message: `${title} message`,
    source_codes: [],
    forensic_facts: {},
    action: { kind: 'wait', label: null, target: null },
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

function render(props: {
  rows: BrokerActivityRow[];
  backfillLoading?: boolean;
  backfillError?: string | null;
  sseStatus?: string;
  sseError?: string | null;
  activityHealth?: BrokerActivityHealth | null;
  eventRows?: ActivityBrokerEventRow[] | null;
}) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(BrokerActivityTableComponent);
  fixture.componentRef.setInput('rows', props.rows);
  fixture.componentRef.setInput('backfillLoading', props.backfillLoading ?? false);
  fixture.componentRef.setInput('backfillError', props.backfillError ?? null);
  fixture.componentRef.setInput('sseStatus', props.sseStatus ?? 'open');
  fixture.componentRef.setInput('sseError', props.sseError ?? null);
  fixture.componentRef.setInput('eventRows', props.eventRows ?? null);
  if (props.activityHealth !== undefined) {
    fixture.componentRef.setInput('activityHealth', props.activityHealth);
  }
  fixture.detectChanges();
  return {
    el: fixture.nativeElement as HTMLElement,
    component: fixture.componentInstance,
    fixture,
  };
}

afterEach(() => TestBed.resetTestingModule());

describe('BrokerActivityTableComponent', () => {
  it('renders the empty state when no executed rows exist', () => {
    const { el } = render({ rows: [] });
    expect(el.querySelector('[data-testid="broker-activity-empty"]')).not.toBeNull();
    expect(el.querySelector('table')).toBeNull();
  });

  it('shows a loading hint while the backfill is in flight', () => {
    const { el } = render({ rows: [], backfillLoading: true });
    expect((el.textContent ?? '').toLowerCase()).toContain('loading history');
  });

  it('surfaces a backfill error when present', () => {
    const { el } = render({
      rows: [],
      backfillError: 'boom',
    });
    const err = el.querySelector('[data-testid="broker-activity-backfill-error"]');
    expect(err).not.toBeNull();
    expect(err?.textContent ?? '').toContain('boom');
  });

  it('renders one row per executed fill with broker-recognisable columns', () => {
    const { el } = render({ rows: [row({ symbol: 'AAPL', quantity: 5, price: 150 })] });
    const body = el.querySelector('tbody');
    expect(body?.textContent ?? '').toContain('AAPL');
    expect(body?.textContent ?? '').toContain('5');
    expect(body?.textContent ?? '').toContain('$150.00');
  });

  it('renders normalized projection event rows when supplied', () => {
    const eventRows: ActivityBrokerEventRow[] = [
      {
        id: 'evidence:1',
        ts_ms: 1_700_000_000_000,
        row_type: 'endpoint_snapshot',
        source: 'account.fetch_positions',
        symbol: null,
        side: null,
        quantity: null,
        price: null,
        status: 'position',
        summary: 'reqPositionsAsync captured by account.fetch_positions',
        verdict: 'evidence',
        replay_count: 1,
        evidence: [
          {
            source: 'account.fetch_positions',
            seq: 1,
            ts_ms: 1_700_000_000_000,
            request_call: 'reqPositionsAsync',
            response_callback: 'position',
          },
        ],
      },
    ];
    const { el } = render({ rows: [], eventRows, sseStatus: 'projection' });
    expect(el.textContent ?? '').toContain('endpoint_snapshot');
    expect(el.textContent ?? '').toContain('reqPositionsAsync captured');
  });

  it('renders the backend-authored narrative is NOT visible until drill-down (verbatim contract)', () => {
    const { el } = render({
      rows: [row({ narrative: 'Backend-authored exact prose XYZ.' })],
    });
    // Narrative is rendered in the drawer (drill-down), not the row.
    expect(el.textContent ?? '').not.toContain('Backend-authored exact prose XYZ.');
  });

  it('expands a row drawer on click and shows the verbatim backend narrative', () => {
    const { el, component, fixture } = render({
      rows: [row({ seq: 7, narrative: 'Exact narrative from backend.' })],
    });
    component.toggleRow(7);
    fixture.detectChanges();
    const drawer = el.querySelector('[data-testid="broker-activity-drawer-7"]');
    expect(drawer).not.toBeNull();
    expect(drawer?.textContent ?? '').toContain('Exact narrative from backend.');
  });

  it('hides engine_only_pending rows from the executed-trades table', () => {
    const { el } = render({
      rows: [
        row({ seq: 1, symbol: 'SPY', verdict: 'engine_only_pending' }),
        row({ seq: 2, symbol: 'AAPL', verdict: 'expected' }),
      ],
    });
    const body = el.querySelector('tbody');
    expect(body?.textContent ?? '').toContain('AAPL');
    expect(body?.textContent ?? '').not.toContain('SPY');
  });

  it.each([
    ['expected', 'verdict-expected'],
    ['expected_with_caveat', 'verdict-caveat'],
    ['unexpected', 'verdict-unexpected'],
    ['engine_only_pending', 'verdict-pending'],
  ] as const)(
    'picks chip class %s -> %s from the closed enum, not from facts',
    (verdict, cls) => {
      const { component } = render({ rows: [] });
      expect(component.verdictClass(verdict)).toBe(cls);
    },
  );

  it('groups consecutive fills under the same perm_id', () => {
    const { el } = render({
      rows: [
        row({ seq: 1, perm_id: 100, exec_id: 'a', quantity: 3 }),
        row({ seq: 2, perm_id: 100, exec_id: 'b', quantity: 7 }),
        row({ seq: 3, perm_id: 200, exec_id: 'c', quantity: 5 }),
      ],
    });
    // Two group headers — Order #100 and Order #200.
    const headers = Array.from(el.querySelectorAll('tr.group-header'));
    expect(headers.length).toBe(2);
    const labels = headers.map((h) => h.textContent?.trim()).filter(Boolean);
    expect(labels).toContain('Order #100');
    expect(labels).toContain('Order #200');
  });

  it('renders unmatched rows (no perm_id) in their own group', () => {
    const { el } = render({
      rows: [row({ perm_id: null, exec_id: 'foreign-exec', symbol: 'TSLA' })],
    });
    const headers = Array.from(el.querySelectorAll('tr.group-header'));
    expect(headers.length).toBe(1);
    expect(headers[0].textContent).toContain('Unmatched');
  });

  it('renders the intent-to-exec lag chip from the backend-provided value (no math)', () => {
    const { el } = render({
      rows: [
        row({
          engine_overlay: {
            intent_id: 'i1',
            mutation_attempt_id: null,
            requested_qty: 10,
            requested_price: null,
            sizing_provenance: null,
            lag_breakdown: {
              intent_to_dispatch_ms: 100,
              dispatch_to_ack_ms: 200,
              ack_to_exec_ms: 50,
              exec_to_observed_ms: 25,
              // Backend stores the chip number — frontend renders it verbatim.
              intent_to_exec_ms: 375,
            },
          },
        }),
      ],
    });
    expect(el.textContent ?? '').toContain('375 ms');
  });

  it('renders — for the lag chip when the backend value is null', () => {
    const { el } = render({ rows: [row({ engine_overlay: null })] });
    const lagCells = el.querySelectorAll('td.num');
    // At least one — for the lag column (the dash also appears elsewhere
    // when nullable numeric fields are missing; we assert presence not count).
    const dashed = Array.from(lagCells).some((c) => c.textContent?.includes('—'));
    expect(dashed).toBe(true);
  });

  // PR 5 — health state rendering

  it('renders health notice and suppresses table when state is unavailable', () => {
    const notice = healthNotice('activity.publisher_not_running', 'critical', 'Activity feed is not running');
    const { el } = render({
      rows: [],
      activityHealth: health('unavailable', notice),
    });
    const healthEl = el.querySelector('[data-testid="broker-activity-health-notice"]');
    expect(healthEl).not.toBeNull();
    expect(healthEl?.textContent ?? '').toContain('Activity feed is not running');
    // Table must not be rendered
    expect(el.querySelector('table')).toBeNull();
  });

  it('renders health notice and suppresses table when state is starting', () => {
    const notice = healthNotice('activity.publisher_starting', 'info', 'Activity feed is starting');
    const { el } = render({
      rows: [],
      activityHealth: health('starting', notice),
    });
    const healthEl = el.querySelector('[data-testid="broker-activity-health-notice"]');
    expect(healthEl).not.toBeNull();
    expect(healthEl?.textContent ?? '').toContain('Activity feed is starting');
    expect(el.querySelector('table')).toBeNull();
  });

  it('renders health notice AND the table when state is degraded', () => {
    const notice = healthNotice('activity.publisher_degraded', 'warning', 'Activity feed is degraded');
    const { el } = render({
      rows: [row()],
      activityHealth: health('degraded', notice),
    });
    const healthEl = el.querySelector('[data-testid="broker-activity-health-notice"]');
    expect(healthEl).not.toBeNull();
    expect(healthEl?.textContent ?? '').toContain('Activity feed is degraded');
    // Table is still rendered when degraded
    expect(el.querySelector('table')).not.toBeNull();
  });

  it('renders table normally when state is ready and no health notice shown', () => {
    const { el } = render({
      rows: [row()],
      activityHealth: health('ready'),
    });
    expect(el.querySelector('[data-testid="broker-activity-health-notice"]')).toBeNull();
    expect(el.querySelector('table')).not.toBeNull();
  });

  it('falls back to Loading history… when activityHealth is null and backfillLoading is true', () => {
    const { el } = render({
      rows: [],
      backfillLoading: true,
      activityHealth: null,
    });
    expect((el.textContent ?? '').toLowerCase()).toContain('loading history');
  });
});
