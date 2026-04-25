/**
 * Auto bar-timeframe heuristic — exhaustive boundary tests.
 *
 * The product-locked rule (2026-04-24):
 *   ≤ 5d → 1m · 5–30d → 5m · 30–120d → 15m · 120d–1y → 1h · > 1y → 1h
 *
 * These assertions pin the boundary at each tier so future edits to the
 * heuristic require an explicit test update.
 */
import { describe, it, expect } from 'vitest';
import { pickAutoBarTimeframe } from './data-lab.component';

describe('pickAutoBarTimeframe', () => {
  it('returns 1-minute for ≤5 days', () => {
    expect(pickAutoBarTimeframe(1)).toBe('1m');
    expect(pickAutoBarTimeframe(5)).toBe('1m');
  });

  it('returns 5-minute for 6–30 days', () => {
    expect(pickAutoBarTimeframe(6)).toBe('5m');
    expect(pickAutoBarTimeframe(30)).toBe('5m');
  });

  it('returns 15-minute for 31–120 days', () => {
    expect(pickAutoBarTimeframe(31)).toBe('15m');
    expect(pickAutoBarTimeframe(120)).toBe('15m');
  });

  it('returns 1-hour for 121–365 days', () => {
    expect(pickAutoBarTimeframe(121)).toBe('1h');
    expect(pickAutoBarTimeframe(365)).toBe('1h');
  });

  it('returns 1-hour for >1 year (Polygon Starter caps at 2y)', () => {
    expect(pickAutoBarTimeframe(500)).toBe('1h');
    expect(pickAutoBarTimeframe(730)).toBe('1h');
  });

  it('returns 1-minute for edge case of 0 days', () => {
    expect(pickAutoBarTimeframe(0)).toBe('1m');
  });
});
