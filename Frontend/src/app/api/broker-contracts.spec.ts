import { describe, expect, it } from 'vitest';
import dataPlaneHealthFixture from '../../../../contracts/fixtures/data-plane-health-v1.json';
import type { components } from './broker.types';

type DataPlaneHealth = components['schemas']['DataPlaneHealth'];

describe('generated broker REST contracts', () => {
  it('keeps the direct FastAPI data-plane health fixture consumable by Angular', () => {
    // Python owns runtime validation of the shared JSON fixture. This assertion
    // pins the Angular-facing generated contract and its int64-ms fields.
    const health = dataPlaneHealthFixture as DataPlaneHealth;

    expect(health.service).toBe('polygon-data-service');
    expect(health.fetched_at_ms).toBeGreaterThanOrEqual(health.process_start_ms);
    expect(health.reload).toBe('watchfiles');
  });
});
