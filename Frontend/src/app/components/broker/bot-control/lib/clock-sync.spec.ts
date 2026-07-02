// PRD #617 — clock-offset utility specs.

import { describe, expect, it } from 'vitest';

import { ClockSync } from './clock-sync';

describe('ClockSync', () => {
  it('reports zero offset before any observation', () => {
    const sync = new ClockSync(() => 1_000);
    const snap = sync.snapshot();
    expect(snap.offsetMs).toBe(0);
    expect(snap.serverNowMs).toBe(1_000);
    expect(snap.advisory).toBe(false);
  });

  it('captures positive offset when server is ahead of client', () => {
    const now = 1_000;
    const sync = new ClockSync(() => now);
    sync.observe(5_000);
    const snap = sync.snapshot();
    expect(snap.offsetMs).toBe(4_000);
    expect(snap.serverNowMs).toBe(5_000);
    expect(snap.advisory).toBe(false);
  });

  it('captures negative offset when client clock is ahead of server', () => {
    const now = 10_000;
    const sync = new ClockSync(() => now);
    sync.observe(8_000);
    const snap = sync.snapshot();
    expect(snap.offsetMs).toBe(-2_000);
    expect(snap.serverNowMs).toBe(8_000);
  });

  it('fires CLOCK DIFFERENCE advisory above the 30-second threshold', () => {
    const sync = new ClockSync(() => 0);
    sync.observe(31_000);
    expect(sync.snapshot().advisory).toBe(true);
  });

  it('stays below threshold at exactly 30 seconds offset', () => {
    const sync = new ClockSync(() => 0);
    sync.observe(30_000);
    expect(sync.snapshot().advisory).toBe(false);
  });

  it('resets drift on a fresh observation (no accumulation)', () => {
    let now = 1_000;
    const sync = new ClockSync(() => now);
    sync.observe(2_000); // offset +1000
    now = 10_000;
    sync.observe(10_500); // fresh offset +500
    expect(sync.snapshot().offsetMs).toBe(500);
  });

  it('schedules boundary refresh at 15s early and 1s after', () => {
    const now = 100_000;
    const sync = new ClockSync(() => now);
    sync.observe(100_000); // offset 0, server time = client time
    const { earlyMs, boundaryMs } = sync.scheduleBoundaryRefresh(160_000);
    expect(earlyMs).toBe(45_000); // 60s away - 15s early
    expect(boundaryMs).toBe(61_000); // 60s away + 1s after
  });

  it('returns null for early refresh when boundary is within 15 seconds', () => {
    const now = 100_000;
    const sync = new ClockSync(() => now);
    sync.observe(100_000);
    const { earlyMs, boundaryMs } = sync.scheduleBoundaryRefresh(110_000);
    expect(earlyMs).toBe(null);
    expect(boundaryMs).toBe(11_000);
  });

  it('returns null for both timers when boundary has already passed', () => {
    const now = 200_000;
    const sync = new ClockSync(() => now);
    sync.observe(200_000);
    const { earlyMs, boundaryMs } = sync.scheduleBoundaryRefresh(100_000);
    expect(earlyMs).toBe(null);
    expect(boundaryMs).toBe(null);
  });

  it('returns null timers when no transition is scheduled', () => {
    const sync = new ClockSync();
    const r = sync.scheduleBoundaryRefresh(null);
    expect(r.earlyMs).toBe(null);
    expect(r.boundaryMs).toBe(null);
  });

  it('uses server-relative time for boundary scheduling even with offset', () => {
    const now = 1_000;
    const sync = new ClockSync(() => now);
    sync.observe(31_000); // offset +30000 (server is 30s ahead)
    // boundary at server-time 91_000 is 60s of server time away;
    // 60s of client time too since the offset is constant.
    const r = sync.scheduleBoundaryRefresh(91_000);
    expect(r.boundaryMs).toBe(61_000);
  });
});
