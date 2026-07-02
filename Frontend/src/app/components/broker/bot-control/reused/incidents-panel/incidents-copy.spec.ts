import { describe, expect, it } from 'vitest';
import {
  INCIDENT_COPY,
  composeIncidentMessage,
  getIncidentCopy,
  getIncidentSourceLabel,
} from './incidents-copy';
import {
  INCIDENT_CATEGORIES,
  INCIDENT_SOURCES,
  type IncidentCategory,
  type IncidentSource,
} from './incidents.types';

describe('INCIDENT_COPY', () => {
  it('has a copy entry for every backend-defined IncidentCategory', () => {
    // The frontend is the rendering source of truth for trader-language
    // strings; if a new category lands in the backend without copy here,
    // the operator sees raw enum tokens. This test guards that contract.
    for (const cat of INCIDENT_CATEGORIES) {
      const copy = INCIDENT_COPY[cat];
      expect(copy, `missing copy for ${cat}`).toBeDefined();
      expect(copy.title).toBeTruthy();
      expect(copy.message).toBeTruthy();
      expect(copy.recommendedAction).toBeTruthy();
      expect(['warning', 'critical', 'blocking', 'unknown']).toContain(copy.severity);
    }
  });

  it('renders the operator-language broker-disconnect copy, not "Error 1100"', () => {
    // VCR-style guard: the panel must not surface raw ib_async strings.
    // The category-resolved copy carries the trader sentence instead.
    const copy = INCIDENT_COPY.broker_disconnect;
    expect(copy.title).toBe('Broker connection lost');
    expect(copy.message).not.toContain('Error 1100');
    expect(copy.message).not.toContain('ib_async');
  });
});

describe('getIncidentCopy', () => {
  it('returns the UNKNOWN copy when the category is null', () => {
    // PR 6 rollout-safety: a backend that omits incident_category falls
    // back to UNKNOWN at the frontend rather than blowing up.
    const copy = getIncidentCopy(null);
    expect(copy).toBe(INCIDENT_COPY.unknown);
  });

  it('returns the UNKNOWN copy when the category is undefined', () => {
    const copy = getIncidentCopy(undefined);
    expect(copy).toBe(INCIDENT_COPY.unknown);
  });

  it('returns the UNKNOWN copy when the backend emits a category the frontend has not seen', () => {
    // Type-cast simulates a future backend enum value that does not exist
    // in the frontend mirror — rollout-safety fallback path.
    const copy = getIncidentCopy('not_in_the_map_yet' as IncidentCategory);
    expect(copy).toBe(INCIDENT_COPY.unknown);
  });

  it('returns the matching copy when the category is known', () => {
    expect(getIncidentCopy('engine_fatal')).toBe(INCIDENT_COPY.engine_fatal);
    expect(getIncidentCopy('lost_fill')).toBe(INCIDENT_COPY.lost_fill);
  });

  it('returns the matching copy for the 6 new (codex 2026-06-24) categories', () => {
    // Round-trip guard: PR-1 added these to the backend enum; if the
    // frontend mirror missed any of them they'd render UNKNOWN copy in
    // production. INCIDENT_CATEGORIES coverage above also catches this,
    // but the explicit list keeps the contract visible in the spec.
    expect(getIncidentCopy('data_farm_degraded')).toBe(INCIDENT_COPY.data_farm_degraded);
    expect(getIncidentCopy('broker_event_log_write_failed')).toBe(
      INCIDENT_COPY.broker_event_log_write_failed,
    );
    expect(getIncidentCopy('foreign_fill_dropped')).toBe(INCIDENT_COPY.foreign_fill_dropped);
    expect(getIncidentCopy('shutdown_flatten_failed')).toBe(
      INCIDENT_COPY.shutdown_flatten_failed,
    );
    expect(getIncidentCopy('control_plane_lease_lost')).toBe(
      INCIDENT_COPY.control_plane_lease_lost,
    );
    expect(getIncidentCopy('sidecar_schema_drift')).toBe(INCIDENT_COPY.sidecar_schema_drift);
  });
});

describe('composeIncidentMessage', () => {
  it('substitutes {name} placeholders from dynamic_facts', () => {
    // Hybrid-C wire shape (D1): backend extracts the typed fact, frontend
    // composes the final sentence. A complete fact map produces a fully
    // rendered string with no leftover braces.
    expect(
      composeIncidentMessage('IBKR data farm degraded (code {tws_code}).', { tws_code: 2103 }),
    ).toBe('IBKR data farm degraded (code 2103).');
  });

  it('leaves the placeholder literal when the fact is missing', () => {
    // Better to surface the gap than to render a broken sentence with an
    // empty slot — operator can still see which fact would have helped.
    expect(composeIncidentMessage('Order {order_id} dropped', {})).toBe(
      'Order {order_id} dropped',
    );
    expect(composeIncidentMessage('Order {order_id} dropped', undefined)).toBe(
      'Order {order_id} dropped',
    );
  });

  it('substitutes only the keys present and leaves unrelated braces alone', () => {
    expect(
      composeIncidentMessage('Path {path} unreadable (extra: {other})', {
        path: '/app/state.json',
      }),
    ).toBe('Path /app/state.json unreadable (extra: {other})');
  });

  it('handles numeric values via String() coercion', () => {
    // Order ids may come back as strings or ints; the function shouldn't
    // care which side picked which encoding.
    expect(composeIncidentMessage('Order {id}', { id: 42 })).toBe('Order 42');
    expect(composeIncidentMessage('Order {id}', { id: '42' })).toBe('Order 42');
  });
});

describe('getIncidentSourceLabel', () => {
  it('has a label entry for every IncidentSource value', () => {
    // Mirrors the backend round-trip test (every IncidentCategory has a
    // _DEFAULT_SOURCE entry). If a new source lands in the backend
    // without a label here, the row would render the UNKNOWN badge.
    for (const source of INCIDENT_SOURCES) {
      const label = getIncidentSourceLabel(source);
      expect(label, `missing label for ${source}`).toBeDefined();
      expect(label.text).toBeTruthy();
      expect(label.longName).toBeTruthy();
      expect(['broker', 'app', 'infra', 'operator', 'unknown']).toContain(label.tone);
    }
  });

  it('returns the UNKNOWN label when the source is null / undefined (D8 rollout safety)', () => {
    // Backend may have rolled out without the source field yet; the
    // panel must badge the row UNKNOWN rather than blow up.
    expect(getIncidentSourceLabel(null).tone).toBe('unknown');
    expect(getIncidentSourceLabel(undefined).tone).toBe('unknown');
  });

  it('returns the UNKNOWN label when the source is not in the closed enum', () => {
    expect(
      getIncidentSourceLabel('not_a_real_source' as IncidentSource).tone,
    ).toBe('unknown');
  });

  it('renders the operator-facing badge text per source', () => {
    expect(getIncidentSourceLabel('broker').text).toBe('BROKER');
    expect(getIncidentSourceLabel('app').text).toBe('APP');
    expect(getIncidentSourceLabel('infra').text).toBe('INFRA');
    // OPERATOR badge reads "YOU" so the operator immediately sees
    // self-initiated state vs system failure.
    expect(getIncidentSourceLabel('operator').text).toBe('YOU');
  });
});
