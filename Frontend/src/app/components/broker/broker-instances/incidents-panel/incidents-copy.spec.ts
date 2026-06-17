import { describe, expect, it } from 'vitest';
import { INCIDENT_COPY, getIncidentCopy } from './incidents-copy';
import { INCIDENT_CATEGORIES, type IncidentCategory } from './incidents.types';

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
});
