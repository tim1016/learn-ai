import type { Logical, LogicalRange } from 'lightweight-charts';
import { describe, expect, it } from 'vitest';
import { isAtLiveEdge } from './bot-trade-chart-card.component';

const range = (from: number, to: number): LogicalRange => ({
  from: from as Logical,
  to: to as Logical,
});

describe('isAtLiveEdge', () => {
  it('treats a null logical range (chart not yet rendered) as at-edge', () => {
    expect(isAtLiveEdge(null, 10)).toBe(true);
  });

  it('treats an empty bar set as at-edge — nothing to scroll back to', () => {
    expect(isAtLiveEdge(range(0, 0), 0)).toBe(true);
  });

  it('returns true when the visible range extends past the last bar', () => {
    // After a new bar lands, lightweight-charts floats range.to slightly past
    // the last logical index. Anything within half a bar of bars.length - 1
    // is still considered the live edge.
    expect(isAtLiveEdge(range(5, 9.2), 10)).toBe(true);
  });

  it('returns true when the visible range matches the last bar exactly', () => {
    expect(isAtLiveEdge(range(5, 9), 10)).toBe(true);
  });

  it('returns false when the user has scrolled back beyond the threshold', () => {
    // Visible range ends three bars before the latest — operator is panning
    // through history; the LIVE pill should dim.
    expect(isAtLiveEdge(range(0, 6), 10)).toBe(false);
  });

  it('honors a custom threshold for callers that need a stricter test', () => {
    // With a zero-bar threshold, range.to must be at or past the last index.
    expect(isAtLiveEdge(range(0, 8.6), 10, 0)).toBe(false);
    expect(isAtLiveEdge(range(0, 9.01), 10, 0)).toBe(true);
  });
});
