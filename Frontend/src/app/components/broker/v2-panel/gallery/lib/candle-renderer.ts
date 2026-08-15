/**
 * Pure canvas candle renderer for the bot gallery wall.
 *
 * No Angular imports. Operates only on plain data and a
 * `CanvasRenderingContext2D` so it is directly unit-testable and reusable
 * outside a component (a future `candle-sparkline`, per
 * `docs/superpowers/specs/2026-08-14-bot-gallery-redesign-design.md` §3.1/§3.2
 * risk note). The caller (`bot-tile.component`) owns the DOM canvas, resize
 * observer, and pointer events; this module owns math and pixels only.
 *
 * Bars are laid out by index, not by time-scale — every column is the same
 * width regardless of any gap between `bars[i].end_ms` and
 * `bars[i + 1].start_ms`, which is what gives the mock its "~100% column
 * width" candles with no reserved axis gutter.
 */
import { TickMarkType } from 'lightweight-charts';

import { formatChartAxisTick } from '../../../../../shared/charts/chart-utils';
import { toCandle } from '../../lib/chart-bar-mapping';
import type { ChartBar, ChartFillMarker } from './gallery.types';

export interface PlotRect {
  readonly left: number;
  readonly top: number;
  readonly width: number;
  readonly height: number;
}

export interface ChartScale {
  readonly lo: number;
  readonly hi: number;
  readonly vmax: number;
  readonly plot: PlotRect;
  readonly tagHalfHeight: number;
}

export interface CandleRendererConfig {
  /** Viewport size for this render pass — the caller overrides both on every resize. */
  readonly width: number;
  readonly height: number;
  readonly padTop: number;
  readonly padRight: number;
  readonly padBottom: number;
  readonly padLeft: number;
  /** Vertical padding fraction applied above/below the bars' high/low span. */
  readonly pricePad: number;
  /** Fraction of the plot height the ghost volume band occupies, anchored to the bottom. */
  readonly volFrac: number;
  readonly gridLineCount: number;
  readonly gridColor: string;
  readonly labelColor: string;
  readonly labelFont: string;
  readonly timeLabelCount: number;
  /** `undefined` = viewer-local, matching the shared formatter's default display mode. */
  readonly timeZone: string | undefined;
  readonly upColor: string;
  readonly downColor: string;
  readonly volUpColor: string;
  readonly volDownColor: string;
  readonly candleGapPx: number;
  readonly tagTextColor: string;
  readonly tagFont: string;
  readonly tagPaddingX: number;
  readonly tagHeight: number;
  readonly tagRadius: number;
  readonly guideDash: readonly number[];
  readonly crosshairColor: string;
  readonly crosshairDash: readonly number[];
  readonly markerBuyColor: string;
  readonly markerSellColor: string;
  readonly markerSize: number;
  readonly markerGap: number;
}

const FONT_MONO =
  '"JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, "Fira Code", monospace';

/**
 * Default config. Colours mirror the literal values of the app's theme
 * tokens (`src/app/styles/_tokens.scss`) rather than `var(--token)` strings
 * — a canvas `fillStyle` cannot resolve CSS custom properties, so the
 * literal hex must stay in sync by hand. Every field is overridable per call
 * (the tile spreads `{ ...CFG, width, height }` on each resize).
 */
export const CFG: CandleRendererConfig = Object.freeze({
  width: 320,
  height: 160,
  padTop: 8,
  padRight: 4,
  padBottom: 4,
  padLeft: 4,
  pricePad: 0.08,
  volFrac: 0.15,
  gridLineCount: 3,
  gridColor: 'rgba(107, 111, 122, 0.18)', // --text-muted at low alpha
  labelColor: '#9598a1', // --text-subtle
  labelFont: `10px ${FONT_MONO}`,
  timeLabelCount: 4,
  timeZone: undefined,
  upColor: '#26a69a', // --bull
  downColor: '#ef5350', // --bear
  volUpColor: 'rgba(38, 166, 154, 0.15)', // --bull-soft
  volDownColor: 'rgba(239, 83, 80, 0.15)', // --bear-soft
  candleGapPx: 1,
  tagTextColor: '#ffffff',
  tagFont: `bold 10px ${FONT_MONO}`,
  tagPaddingX: 4,
  tagHeight: 16,
  tagRadius: 3,
  guideDash: [3, 3],
  crosshairColor: '#6b6f7a', // --text-muted
  crosshairDash: [2, 3],
  markerBuyColor: '#2962ff', // --accent
  markerSellColor: '#ff9800', // --warn
  markerSize: 5,
  markerGap: 3,
});

/**
 * Price/volume bounds and the plot rect for `bars`, padded `cfg.pricePad`
 * above/below the high/low span. Deterministic given the same input.
 */
export function computeScale(
  bars: readonly ChartBar[],
  cfg: CandleRendererConfig = CFG,
): ChartScale {
  const plot: PlotRect = {
    left: cfg.padLeft,
    top: cfg.padTop,
    width: Math.max(0, cfg.width - cfg.padLeft - cfg.padRight),
    height: Math.max(0, cfg.height - cfg.padTop - cfg.padBottom),
  };

  let min = Infinity;
  let max = -Infinity;
  let vmax = 0;
  for (const bar of bars) {
    const { high, low } = toCandle(bar);
    if (high > max) max = high;
    if (low < min) min = low;
    if (bar.volume > vmax) vmax = bar.volume;
  }

  if (!Number.isFinite(min) || !Number.isFinite(max)) {
    return { lo: 0, hi: 1, vmax: 0, plot, tagHalfHeight: cfg.tagHeight / 2 };
  }

  const span = max - min;
  // A flat (or single-bar) price series collapses lo === hi; fall back to
  // padding off the price magnitude so the scale never divides by zero.
  const pad = span > 0 ? span * cfg.pricePad : Math.max(Math.abs(max), 1) * cfg.pricePad;

  return {
    lo: min - pad,
    hi: max + pad,
    vmax,
    plot,
    tagHalfHeight: cfg.tagHeight / 2,
  };
}

/** Bar index under `x`, clamped to `[0, barCount - 1]`. */
export function barIndexAtX(x: number, scale: ChartScale, barCount: number): number {
  if (barCount <= 0) return 0;
  const barWidth = scale.plot.width / barCount;
  if (!Number.isFinite(barWidth) || barWidth <= 0) return 0;
  const idx = Math.floor((x - scale.plot.left) / barWidth);
  return Math.min(barCount - 1, Math.max(0, idx));
}

function priceToY(price: number, scale: ChartScale): number {
  const span = scale.hi - scale.lo;
  if (span <= 0) return scale.plot.top + scale.plot.height / 2;
  const frac = (price - scale.lo) / span;
  return scale.plot.top + scale.plot.height * (1 - frac);
}

/**
 * Anchor for the floating last-price tag: horizontally pinned to the plot's
 * right edge (bars fill the plot edge-to-edge), vertically clamped so the
 * tag box never clips outside the plot's top/bottom.
 */
export function layoutTag(lastClose: number, scale: ChartScale): { x: number; y: number } {
  const rawY = priceToY(lastClose, scale);
  const minY = scale.plot.top + scale.tagHalfHeight;
  const maxY = scale.plot.top + scale.plot.height - scale.tagHalfHeight;
  const y = Math.min(maxY, Math.max(minY, rawY));
  return { x: scale.plot.left + scale.plot.width, y };
}

function formatPrice(value: number): string {
  return value.toFixed(2);
}

/** First bar index whose `[start_ms, end_ms)` window contains `ms`; clamps at both ends. */
function findBarIndex(bars: readonly ChartBar[], ms: number): number {
  for (let i = 0; i < bars.length; i++) {
    if (ms < bars[i].end_ms) return i;
  }
  return bars.length - 1;
}

/** `count` bar indices spread evenly across `[0, barCount - 1]`, deduped. */
function sparseIndices(barCount: number, count: number): number[] {
  if (barCount <= 0) return [];
  if (barCount === 1 || count <= 1) return [0];
  const picked = new Set<number>();
  for (let i = 0; i < count; i++) {
    picked.add(Math.round((i * (barCount - 1)) / (count - 1)));
  }
  return [...picked];
}

function roundedRectPath(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
): void {
  const rr = Math.max(0, Math.min(r, w / 2, h / 2));
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + h, rr);
  ctx.arcTo(x + w, y + h, x, y + h, rr);
  ctx.arcTo(x, y + h, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}

function drawTriangle(
  ctx: CanvasRenderingContext2D,
  x: number,
  apexY: number,
  size: number,
  pointsUp: boolean,
  color: string,
): void {
  const baseY = pointsUp ? apexY + size : apexY - size;
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.moveTo(x, apexY);
  ctx.lineTo(x - size, baseY);
  ctx.lineTo(x + size, baseY);
  ctx.closePath();
  ctx.fill();
}

function drawGrid(ctx: CanvasRenderingContext2D, scale: ChartScale, cfg: CandleRendererConfig): void {
  ctx.setLineDash([]);
  ctx.lineWidth = 1;
  ctx.font = cfg.labelFont;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'bottom';
  for (let k = 1; k <= cfg.gridLineCount; k++) {
    const frac = k / (cfg.gridLineCount + 1);
    const y = scale.plot.top + scale.plot.height * frac;
    const price = scale.hi - (scale.hi - scale.lo) * frac;

    ctx.strokeStyle = cfg.gridColor;
    ctx.beginPath();
    ctx.moveTo(scale.plot.left, y);
    ctx.lineTo(scale.plot.left + scale.plot.width, y);
    ctx.stroke();

    ctx.fillStyle = cfg.labelColor;
    ctx.fillText(formatPrice(price), scale.plot.left + scale.plot.width - 2, y - 2);
  }
}

function drawVolumeAndCandles(
  ctx: CanvasRenderingContext2D,
  bars: readonly ChartBar[],
  scale: ChartScale,
  cfg: CandleRendererConfig,
): number {
  const barWidth = scale.plot.width / bars.length;
  const bodyWidth = Math.max(1, barWidth - cfg.candleGapPx);
  const bottom = scale.plot.top + scale.plot.height;

  for (let i = 0; i < bars.length; i++) {
    const { open, high, low, close } = toCandle(bars[i]);
    const xCenter = scale.plot.left + i * barWidth + barWidth / 2;
    const up = close >= open;

    const volH = scale.vmax > 0 ? (bars[i].volume / scale.vmax) * scale.plot.height * cfg.volFrac : 0;
    ctx.fillStyle = up ? cfg.volUpColor : cfg.volDownColor;
    ctx.fillRect(xCenter - bodyWidth / 2, bottom - volH, bodyWidth, volH);

    const color = up ? cfg.upColor : cfg.downColor;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.setLineDash([]);
    ctx.beginPath();
    ctx.moveTo(xCenter, priceToY(high, scale));
    ctx.lineTo(xCenter, priceToY(low, scale));
    ctx.stroke();

    ctx.fillStyle = color;
    const yOpen = priceToY(open, scale);
    const yClose = priceToY(close, scale);
    const bodyTop = Math.min(yOpen, yClose);
    const bodyHeight = Math.max(1, Math.abs(yClose - yOpen));
    ctx.fillRect(xCenter - bodyWidth / 2, bodyTop, bodyWidth, bodyHeight);
  }

  return barWidth;
}

function drawMarkers(
  ctx: CanvasRenderingContext2D,
  bars: readonly ChartBar[],
  markers: readonly ChartFillMarker[],
  scale: ChartScale,
  barWidth: number,
  cfg: CandleRendererConfig,
): void {
  for (const marker of markers) {
    const idx = findBarIndex(bars, marker.filled_at_ms);
    const { high, low } = toCandle(bars[idx]);
    const xCenter = scale.plot.left + idx * barWidth + barWidth / 2;
    const isBuy = marker.side === 'buy';
    const apexY = isBuy
      ? priceToY(low, scale) + cfg.markerGap
      : priceToY(high, scale) - cfg.markerGap;
    drawTriangle(ctx, xCenter, apexY, cfg.markerSize, isBuy, isBuy ? cfg.markerBuyColor : cfg.markerSellColor);
  }
}

function drawTimeLabels(
  ctx: CanvasRenderingContext2D,
  bars: readonly ChartBar[],
  scale: ChartScale,
  barWidth: number,
  cfg: CandleRendererConfig,
): void {
  ctx.fillStyle = cfg.labelColor;
  ctx.font = cfg.labelFont;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'bottom';
  const y = scale.plot.top + scale.plot.height - 2;
  for (const idx of sparseIndices(bars.length, cfg.timeLabelCount)) {
    const xCenter = scale.plot.left + idx * barWidth + barWidth / 2;
    const seconds = toCandle(bars[idx]).time;
    ctx.fillText(formatChartAxisTick(seconds, cfg.timeZone, TickMarkType.Time), xCenter, y);
  }
}

function drawCrosshair(
  ctx: CanvasRenderingContext2D,
  scale: ChartScale,
  barWidth: number,
  hoverIndex: number,
  cfg: CandleRendererConfig,
): void {
  const xCenter = scale.plot.left + hoverIndex * barWidth + barWidth / 2;
  ctx.strokeStyle = cfg.crosshairColor;
  ctx.lineWidth = 1;
  ctx.setLineDash([...cfg.crosshairDash]);
  ctx.beginPath();
  ctx.moveTo(xCenter, scale.plot.top);
  ctx.lineTo(xCenter, scale.plot.top + scale.plot.height);
  ctx.stroke();
  ctx.setLineDash([]);
}

/**
 * The last-price tag reads only `bars[bars.length - 1].close` — never a
 * separately fetched live quote — so the painted value can never disagree
 * with the last drawn candle (temporal-rigor.md display discipline). Session
 * direction (tag colour) compares `bars[0].open` to that same close, not the
 * last bar's own direction.
 */
function drawLastPriceTag(
  ctx: CanvasRenderingContext2D,
  bars: readonly ChartBar[],
  scale: ChartScale,
  cfg: CandleRendererConfig,
): void {
  const lastClose = toCandle(bars[bars.length - 1]).close;
  const firstOpen = toCandle(bars[0]).open;
  const color = lastClose >= firstOpen ? cfg.upColor : cfg.downColor;
  const { x, y } = layoutTag(lastClose, scale);

  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  ctx.setLineDash([...cfg.guideDash]);
  ctx.beginPath();
  ctx.moveTo(scale.plot.left, y);
  ctx.lineTo(x, y);
  ctx.stroke();
  ctx.setLineDash([]);

  const label = formatPrice(lastClose);
  ctx.font = cfg.tagFont;
  const boxWidth = ctx.measureText(label).width + cfg.tagPaddingX * 2;
  const boxX = x - boxWidth;
  const boxY = y - cfg.tagHeight / 2;

  roundedRectPath(ctx, boxX, boxY, boxWidth, cfg.tagHeight, cfg.tagRadius);
  ctx.fillStyle = color;
  ctx.fill();

  ctx.fillStyle = cfg.tagTextColor;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(label, boxX + boxWidth / 2, y);
}

/**
 * Full repaint: grid + inside price labels, ghost volume band, candles,
 * fill markers, sparse time labels, crosshair, then the floating
 * last-price tag on top.
 */
export function draw(
  ctx: CanvasRenderingContext2D,
  bars: readonly ChartBar[],
  markers: readonly ChartFillMarker[],
  scale: ChartScale,
  hoverIndex: number | null,
  cfg: CandleRendererConfig = CFG,
): void {
  ctx.clearRect(0, 0, cfg.width, cfg.height);
  if (bars.length === 0) return;

  drawGrid(ctx, scale, cfg);
  const barWidth = drawVolumeAndCandles(ctx, bars, scale, cfg);
  drawMarkers(ctx, bars, markers, scale, barWidth, cfg);
  drawTimeLabels(ctx, bars, scale, barWidth, cfg);
  if (hoverIndex !== null && hoverIndex >= 0 && hoverIndex < bars.length) {
    drawCrosshair(ctx, scale, barWidth, hoverIndex, cfg);
  }
  drawLastPriceTag(ctx, bars, scale, cfg);
}
