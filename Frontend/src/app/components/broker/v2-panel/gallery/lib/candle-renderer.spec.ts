import { describe, expect, it, vi } from 'vitest';

import {
  CFG,
  barIndexAtX,
  computeScale,
  draw,
  layoutTag,
  type CandleRendererConfig,
  type ChartScale,
} from './candle-renderer';
import type { ChartBar, ChartFillMarker } from './gallery.types';

function bar(overrides: Partial<ChartBar> = {}): ChartBar {
  return {
    start_ms: 1_700_000_000_000,
    end_ms: 1_700_000_060_000,
    open: '100.00',
    high: '101.00',
    low: '99.00',
    close: '100.50',
    volume: 1_000,
    source: 'polygon',
    ...overrides,
  };
}

function marker(overrides: Partial<ChartFillMarker> = {}): ChartFillMarker {
  return {
    filled_at_ms: 1_700_000_030_000,
    side: 'buy',
    quantity: 10,
    price: 100.25,
    order_ref: 'ord-1',
    event_key: 'exec-1',
    ...overrides,
  };
}

/** Minimal stub of the `CanvasRenderingContext2D` surface `draw` calls. */
function createStubCtx(): CanvasRenderingContext2D {
  const stub = {
    fillStyle: '',
    strokeStyle: '',
    lineWidth: 1,
    font: '',
    textAlign: 'left' as CanvasTextAlign,
    textBaseline: 'alphabetic' as CanvasTextBaseline,
    clearRect: vi.fn(),
    setLineDash: vi.fn(),
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    arcTo: vi.fn(),
    closePath: vi.fn(),
    stroke: vi.fn(),
    fill: vi.fn(),
    fillRect: vi.fn(),
    fillText: vi.fn(),
    measureText: vi.fn((text: string) => ({ width: text.length * 6 })),
  };
  return stub as unknown as CanvasRenderingContext2D;
}

describe('computeScale', () => {
  it('returns the exact high/low bounds padded by 8%, and the max volume', () => {
    const bars = [
      bar({ high: '110.00', low: '95.00', volume: 1_000 }),
      bar({ high: '108.00', low: '90.00', volume: 1_500 }),
    ];

    const scale = computeScale(bars, CFG);

    // min=90, max=110, span=20, pad=20*0.08=1.6 -> lo=88.4, hi=111.6
    expect(scale.lo).toBeCloseTo(88.4, 9);
    expect(scale.hi).toBeCloseTo(111.6, 9);
    expect(scale.vmax).toBe(1_500);
    expect(scale.plot).toEqual({
      left: CFG.padLeft,
      top: CFG.padTop,
      width: CFG.width - CFG.padLeft - CFG.padRight,
      height: CFG.height - CFG.padTop - CFG.padBottom,
    });
    expect(scale.tagHalfHeight).toBe(CFG.tagHeight / 2);
  });

  it('pads off the price magnitude, not the (zero) span, for a flat price series', () => {
    const bars = [bar({ open: '50.00', high: '50.00', low: '50.00', close: '50.00' })];

    const scale = computeScale(bars, CFG);

    // span=0 -> pad = |50| * 0.08 = 4 -> lo=46, hi=54
    expect(scale.lo).toBeCloseTo(46, 9);
    expect(scale.hi).toBeCloseTo(54, 9);
  });

  it('falls back to a fixed lo/hi/vmax for an empty bar array', () => {
    const scale = computeScale([], CFG);

    expect(scale.lo).toBe(0);
    expect(scale.hi).toBe(1);
    expect(scale.vmax).toBe(0);
  });
});

describe('barIndexAtX', () => {
  const cfg: CandleRendererConfig = {
    ...CFG,
    width: 100,
    height: 100,
    padLeft: 0,
    padRight: 0,
    padTop: 0,
    padBottom: 0,
  };
  const scale = computeScale([bar()], cfg);
  const barCount = 5; // barWidth = 100 / 5 = 20

  it.each([
    [0, 0],
    [19, 0],
    [20, 1],
    [39, 1],
    [40, 2],
    [99, 4],
  ])('maps x=%d to bar index %d', (x, expected) => {
    expect(barIndexAtX(x, scale, barCount)).toBe(expected);
  });

  it('clamps at the right edge, including out-of-bounds x', () => {
    expect(barIndexAtX(100, scale, barCount)).toBe(4);
    expect(barIndexAtX(1_000, scale, barCount)).toBe(4);
  });

  it('clamps at the left edge for negative x', () => {
    expect(barIndexAtX(-50, scale, barCount)).toBe(0);
  });

  it('returns 0 for a zero bar count', () => {
    expect(barIndexAtX(50, scale, 0)).toBe(0);
  });
});

describe('layoutTag', () => {
  const scale: ChartScale = {
    lo: 0,
    hi: 100,
    vmax: 1_000,
    plot: { left: 0, top: 0, width: 100, height: 100 },
    tagHalfHeight: 10,
  };

  it('pins x to the plot right edge and y to the mapped price mid-range', () => {
    expect(layoutTag(50, scale)).toEqual({ x: 100, y: 50 });
  });

  it('clamps y at the top extreme so the tag never clips above the plot', () => {
    expect(layoutTag(150, scale)).toEqual({ x: 100, y: 10 });
  });

  it('clamps y at the bottom extreme so the tag never clips below the plot', () => {
    expect(layoutTag(-50, scale)).toEqual({ x: 100, y: 90 });
  });
});

describe('draw', () => {
  it('does not throw for an empty bar array', () => {
    const ctx = createStubCtx();
    const scale = computeScale([], CFG);

    expect(() => draw(ctx, [], [], scale, null, CFG)).not.toThrow();
  });

  it('does not throw for a single-bar array with a marker on it', () => {
    const ctx = createStubCtx();
    const bars = [bar()];
    const markers = [marker({ filled_at_ms: bars[0].start_ms + 1 })];
    const scale = computeScale(bars, CFG);

    expect(() => draw(ctx, bars, markers, scale, 0, CFG)).not.toThrow();
  });

  it('does not throw for an all-flat-price array', () => {
    const ctx = createStubCtx();
    const bars = Array.from({ length: 5 }, (_, i) =>
      bar({
        start_ms: 1_700_000_000_000 + i * 60_000,
        end_ms: 1_700_000_000_000 + (i + 1) * 60_000,
        open: '100.00',
        high: '100.00',
        low: '100.00',
        close: '100.00',
        volume: 0,
      }),
    );
    const scale = computeScale(bars, CFG);

    expect(() => draw(ctx, bars, [], scale, null, CFG)).not.toThrow();
  });

  it('does not throw with buy and sell markers plus an active hover index', () => {
    const ctx = createStubCtx();
    const bars = Array.from({ length: 8 }, (_, i) =>
      bar({
        start_ms: 1_700_000_000_000 + i * 60_000,
        end_ms: 1_700_000_000_000 + (i + 1) * 60_000,
        open: String(100 + i),
        high: String(101 + i),
        low: String(99 + i),
        close: String(100.5 + i),
        volume: 100 * (i + 1),
      }),
    );
    const markers = [
      marker({ side: 'buy', filled_at_ms: bars[2].start_ms + 1 }),
      marker({ side: 'sell', filled_at_ms: bars[5].start_ms + 1, order_ref: 'ord-2' }),
    ];
    const scale = computeScale(bars, CFG);

    expect(() => draw(ctx, bars, markers, scale, 4, CFG)).not.toThrow();
  });

  it('skips a marker outside every buffered bar window instead of clamping it onto an edge bar', () => {
    // ctx.fill() is called exactly once per draw for the last-price tag,
    // plus once more per drawn marker triangle (the only other `fill()`
    // caller) — so the call count is a direct, unambiguous proxy for "was
    // this marker actually drawn".
    const bars = Array.from({ length: 4 }, (_, i) =>
      bar({ start_ms: 1_700_000_000_000 + i * 60_000, end_ms: 1_700_000_000_000 + (i + 1) * 60_000 }),
    );
    const scale = computeScale(bars, CFG);

    const tagOnly = createStubCtx();
    draw(tagOnly, bars, [], scale, null, CFG);
    expect(tagOnly.fill).toHaveBeenCalledTimes(1);

    const withInWindowMarker = createStubCtx();
    draw(withInWindowMarker, bars, [marker({ filled_at_ms: bars[1].start_ms + 1 })], scale, null, CFG);
    expect(withInWindowMarker.fill).toHaveBeenCalledTimes(2);

    const withNewerThanBuffer = createStubCtx();
    draw(
      withNewerThanBuffer,
      bars,
      [marker({ filled_at_ms: bars[bars.length - 1].end_ms + 60_000 })], // the forming, not-yet-closed minute
      scale,
      null,
      CFG,
    );
    expect(withNewerThanBuffer.fill).toHaveBeenCalledTimes(1);

    const withOlderThanBuffer = createStubCtx();
    draw(
      withOlderThanBuffer,
      bars,
      [marker({ filled_at_ms: bars[0].start_ms - 60_000 })], // trimmed out of the buffer
      scale,
      null,
      CFG,
    );
    expect(withOlderThanBuffer.fill).toHaveBeenCalledTimes(1);

    const barsWithGap = [
      bar({ start_ms: 1_700_000_000_000, end_ms: 1_700_000_060_000 }),
      bar({ start_ms: 1_700_000_120_000, end_ms: 1_700_000_180_000 }),
    ];
    const gapScale = computeScale(barsWithGap, CFG);
    const withMarkerInGap = createStubCtx();
    draw(withMarkerInGap, barsWithGap, [marker({ filled_at_ms: 1_700_000_090_000 })], gapScale, null, CFG);
    expect(withMarkerInGap.fill).toHaveBeenCalledTimes(1);
  });
});
