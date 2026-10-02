import { describe, expect, it, vi } from 'vitest';

import { StrategyChartOverlay, logicalIndexAt, type OverlayBar } from './strategy-chart-overlay';

const MINUTE = 60_000;
/** Three 15-minute bars, the third after a 30-minute gap (a missing bar). */
const BARS: readonly OverlayBar[] = [
  { startMs: 0, closeMs: 15 * MINUTE },
  { startMs: 15 * MINUTE, closeMs: 30 * MINUTE },
  { startMs: 60 * MINUTE, closeMs: 75 * MINUTE },
];

describe('logicalIndexAt', () => {
  it('puts a bar’s close on its candle’s right edge and an instant inside a bar inside its candle', () => {
    expect(logicalIndexAt(BARS, 15 * MINUTE)).toBe(0.5);
    // Five seconds into the second bar: just right of the first candle — where "Bot started" belongs.
    expect(logicalIndexAt(BARS, 15 * MINUTE + 5_000)).toBeCloseTo(0.5 + 5_000 / (15 * MINUTE), 12);
    expect(logicalIndexAt(BARS, 22.5 * MINUTE)).toBe(1);
  });

  it('lands an instant in a gap between bars on their boundary', () => {
    expect(logicalIndexAt(BARS, 45 * MINUTE)).toBe(1.5);
  });

  it('extends past either end at that end bar’s length, at most one bar out, and has no place without bars', () => {
    expect(logicalIndexAt(BARS, -7.5 * MINUTE)).toBe(-1);
    expect(logicalIndexAt(BARS, 82.5 * MINUTE)).toBe(3);
    // A run stopped hours after its last bar: its line stays beside that bar.
    expect(logicalIndexAt(BARS, -120 * MINUTE)).toBe(-1.5);
    expect(logicalIndexAt(BARS, 300 * MINUTE)).toBe(3.5);
    expect(logicalIndexAt([], 0)).toBeNull();
  });
});

/** Like lightweight-charts: whole bar indices only — a fractional one comes back as 0. */
function wholeBarCoordinate(logical: number): number {
  return Number.isInteger(logical) ? 100 + logical * 20 : 0;
}

describe('StrategyChartOverlay', () => {
  /** Draws the overlay on a stand-in canvas whose labels measure `labelWidth` each. */
  function drawn(overlay: StrategyChartOverlay, labelWidth = 60) {
    const context = {
      fillRect: vi.fn(), strokeRect: vi.fn(), fillText: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(),
      beginPath: vi.fn(), stroke: vi.fn(), setLineDash: vi.fn(), measureText: () => ({ width: labelWidth }),
    };
    const target = {
      useMediaCoordinateSpace: (draw: (scope: unknown) => void) => draw({ context, mediaSize: { width: 800, height: 300 } }),
    };
    for (const view of overlay.paneViews()) {
      view.renderer()?.draw(target as never);
    }
    return context;
  }

  it('shades up to the start line and labels both run lines at their instants', () => {
    const overlay = new StrategyChartOverlay(true);
    const requestUpdate = vi.fn();
    overlay.attached({
      chart: { timeScale: () => ({ logicalToCoordinate: wholeBarCoordinate }) },
      requestUpdate,
    } as never);

    overlay.update({
      bars: BARS,
      shadeBeforeMs: 15 * MINUTE,
      shadeLabel: 'Before start · not acted on',
      lines: [
        { atMs: 15 * MINUTE, label: 'Bot started 13:30', emphasis: 'start' },
        { atMs: 75 * MINUTE, label: 'Ended 14:30', emphasis: 'end' },
      ],
      highlightCloseMs: 30 * MINUTE,
    });
    const context = drawn(overlay);

    expect(requestUpdate).toHaveBeenCalled();
    // Shade from the left edge to the first candle's right edge (logical 0.5 → x 110).
    expect(context.fillRect).toHaveBeenCalledWith(0, 0, 110, 300);
    // The selected (second) candle's band spans logical 0.5–1.5.
    expect(context.fillRect).toHaveBeenCalledWith(110, 0, 20, 300);
    // The shade's label ends at the start line; the start line's reads rightwards from it.
    expect(context.fillText).toHaveBeenCalledWith('Before start · not acted on', 106, 12);
    expect(context.fillText).toHaveBeenCalledWith('Bot started 13:30', 114, 12);
    expect(context.fillText).toHaveBeenCalledWith('Ended 14:30', 146, 25);
  });

  it('leaves the shade’s label out when the shaded strip cannot hold it', () => {
    const overlay = new StrategyChartOverlay(true);
    overlay.attached({
      chart: { timeScale: () => ({ logicalToCoordinate: wholeBarCoordinate }) },
      requestUpdate: vi.fn(),
    } as never);
    overlay.update({
      bars: BARS,
      shadeBeforeMs: 15 * MINUTE,
      shadeLabel: 'Before start · not acted on',
      lines: [{ atMs: 15 * MINUTE, label: 'Bot started 13:30', emphasis: 'start' }],
      highlightCloseMs: null,
    });
    const context = drawn(overlay, 150);

    expect(context.fillRect).toHaveBeenCalledWith(0, 0, 110, 300);
    expect(context.fillText.mock.calls.map(([text]) => text)).toEqual(['Bot started 13:30']);
  });

  it('repeats the shade and lines on a lower pane without the labels', () => {
    const overlay = new StrategyChartOverlay(false);
    overlay.attached({
      chart: { timeScale: () => ({ logicalToCoordinate: wholeBarCoordinate }) },
      requestUpdate: vi.fn(),
    } as never);
    overlay.update({
      bars: BARS,
      shadeBeforeMs: 15 * MINUTE,
      shadeLabel: 'Before start · not acted on',
      lines: [{ atMs: 15 * MINUTE, label: 'Bot started 13:30', emphasis: 'start' }],
      highlightCloseMs: null,
    });
    const context = drawn(overlay);

    expect(context.fillRect).toHaveBeenCalledWith(0, 0, 110, 300);
    expect(context.lineTo).toHaveBeenCalledWith(110, 300);
    expect(context.fillText).not.toHaveBeenCalled();
  });
});
