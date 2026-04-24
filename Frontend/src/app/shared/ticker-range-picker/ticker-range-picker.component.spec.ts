/**
 * TickerRangePicker — pure-function tests.
 *
 * Covers the two pieces of logic that drive the component's behavior:
 * ``summarizeAvailability`` (counts cells by status, ignores weekends)
 * and ``computeAdvisories`` (smart-combination hints — resolution
 * downgrade, missing-data auto-fetch, wide-range warnings).
 *
 * Rendering is intentionally not tested here: the project does not
 * ship ``@testing-library/angular``, and the component's visual output
 * is 1:1 with its inputs via signals + `@for` — no branching worth
 * asserting on at the markup level.
 */
import { computeAdvisories, summarizeAvailability } from './ticker-range-picker.types';

describe('summarizeAvailability', () => {
  it('counts cells by status and ignores weekends', () => {
    const s = summarizeAvailability([
      { date: '2026-01-01', status: 'complete' },
      { date: '2026-01-02', status: 'complete' },
      { date: '2026-01-03', status: 'weekend' },
      { date: '2026-01-04', status: 'weekend' },
      { date: '2026-01-05', status: 'partial' },
      { date: '2026-01-06', status: 'hole' },
      { date: '2026-01-07', status: 'missing' },
    ]);
    expect(s).toEqual({ complete: 2, partial: 1, hole: 1, missing: 1, weekdays: 5 });
  });

  it('returns zeros for an empty cell list', () => {
    expect(summarizeAvailability([])).toEqual({
      complete: 0, partial: 0, hole: 0, missing: 0, weekdays: 0,
    });
  });
});

describe('computeAdvisories', () => {
  it('suggests hour bars when minute range > 90 days', () => {
    const advisories = computeAdvisories(
      { symbol: 'SPY', from: '2025-01-01', to: '2026-04-01', resolution: 'minute' },
      { complete: 100, partial: 0, hole: 0, missing: 0, weekdays: 100 },
    );
    expect(
      advisories.some((a) => a.kind === 'suggest' && a.action?.patch?.resolution === 'hour'),
    ).toBe(true);
  });

  it('warns when data is missing and auto-fetch is off', () => {
    const advisories = computeAdvisories(
      { symbol: 'SPY', from: '2026-04-01', to: '2026-04-23', resolution: 'daily', autoFetch: false },
      { complete: 5, partial: 0, hole: 0, missing: 3, weekdays: 8 },
    );
    expect(advisories.some((a) => a.kind === 'warn' && a.action?.triggerRun)).toBe(true);
  });

  it('does not warn about missing data when auto-fetch is on', () => {
    const advisories = computeAdvisories(
      { symbol: 'SPY', from: '2026-04-01', to: '2026-04-23', resolution: 'daily', autoFetch: true },
      { complete: 5, partial: 0, hole: 0, missing: 3, weekdays: 8 },
    );
    expect(advisories.some((a) => a.kind === 'warn')).toBe(false);
  });

  it('bad-marks a minute range > 365 days', () => {
    const advisories = computeAdvisories(
      { symbol: 'SPY', from: '2024-01-01', to: '2026-04-01', resolution: 'minute' },
      { complete: 500, partial: 0, hole: 0, missing: 0, weekdays: 500 },
    );
    expect(advisories.some((a) => a.kind === 'bad')).toBe(true);
  });

  it('info-notes when no cache for the symbol exists in the range', () => {
    const advisories = computeAdvisories(
      { symbol: 'SPY', from: '2026-04-01', to: '2026-04-17', resolution: 'daily' },
      { complete: 0, partial: 0, hole: 0, missing: 0, weekdays: 13 },
    );
    expect(advisories.some((a) => a.kind === 'info')).toBe(true);
  });

  it('emits no advisories in the happy path', () => {
    const advisories = computeAdvisories(
      { symbol: 'SPY', from: '2026-04-01', to: '2026-04-23', resolution: 'daily', autoFetch: false },
      { complete: 17, partial: 0, hole: 0, missing: 0, weekdays: 17 },
    );
    expect(advisories).toEqual([]);
  });
});
