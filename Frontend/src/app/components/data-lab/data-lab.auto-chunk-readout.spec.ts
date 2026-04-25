/**
 * Auto chunk readout — wording must stay plan-tier-agnostic so the
 * UI is correct for both Polygon Starter (free, paced 5/min) and the
 * paid tiers (back-to-back, no per-minute cap). The product copy
 * locked on 2026-04-24 (see fix(data-lab): drop misleading "5 req/min"
 * copy from plan banner & readout) — these tests pin that wording.
 */
import { describe, it, expect } from 'vitest';
import { formatChunkReadout } from './data-lab.component';

describe('formatChunkReadout', () => {
  it('renders the manual variant when autoChunk is off', () => {
    const out = formatChunkReadout(120_000, false, 50_000);
    expect(out).toContain('Manual');
    expect(out).toContain('50,000');
    expect(out).not.toContain('paced');
  });

  it('renders the single-request variant when one chunk covers the range', () => {
    const out = formatChunkReadout(40_000, true, 50_000);
    expect(out).toBe('1 request · ~40,000 bars · single response.');
  });

  it('renders the multi-chunk variant with bars locale-formatted', () => {
    const out = formatChunkReadout(120_000, true, 50_000);
    // ceil(120000 / 50000) = 3 chunks
    expect(out).toContain('Plan runs 3 requests');
    expect(out).toContain('~120,000 bars');
  });

  it('does not quote a specific per-minute Polygon cap (plan-tier-agnostic)', () => {
    const out = formatChunkReadout(200_000, true, 50_000);
    // Pin: the legacy copy mentioned "5 req/min"; the redesign drops it.
    expect(out).not.toMatch(/\d\s*req\/min/);
    expect(out).not.toMatch(/per\s*minute/);
    // The new copy uses "paced if your plan caps requests/min" — flexible
    // about whether pacing actually fires.
    expect(out).toContain('paced if your plan caps requests/min');
  });

  it('rounds chunk count up — 50,001 bars → 2 chunks', () => {
    const out = formatChunkReadout(50_001, true, 50_000);
    expect(out).toContain('Plan runs 2 requests');
  });

  it('clamps chunks to a minimum of 1 even for a zero-bar projection', () => {
    const out = formatChunkReadout(0, true, 50_000);
    expect(out).toContain('1 request');
  });

  it('drives chunk count off polygonLimit, not a hard-coded 50,000', () => {
    // 30,000 bars at the default 50k limit fits in one request, but at a
    // 10k per-request limit needs three. Pin: the readout reflects the
    // configured limit so a future UI knob does not silently lie.
    expect(formatChunkReadout(30_000, true, 10_000)).toContain('Plan runs 3 requests');
    expect(formatChunkReadout(30_000, true, 50_000)).toBe('1 request · ~30,000 bars · single response.');
  });

  it('treats a non-positive polygonLimit as 1 to avoid division blow-ups', () => {
    // Defensive guard — callers should never supply 0, but if they do the
    // function must produce a finite chunk count rather than NaN/Infinity.
    const out = formatChunkReadout(100, true, 0);
    expect(out).toContain('Plan runs 100 requests');
  });
});
