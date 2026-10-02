import type {
  IChartApiBase,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesPrimitive,
  Logical,
  SeriesAttachedParameter,
  Time,
} from 'lightweight-charts';

/** The canvas target lightweight-charts hands a primitive renderer. */
type RenderingTarget = Parameters<IPrimitivePaneRenderer['draw']>[0];

/** One decision bar's interval: the candle is labelled by its close. */
export interface OverlayBar {
  readonly startMs: number;
  readonly closeMs: number;
}

/** A vertical line at an instant, e.g. "Bot started 13:30", or the running bot's "Now". */
export interface OverlayLine {
  readonly atMs: number;
  readonly label: string;
  readonly emphasis: 'start' | 'end' | 'now';
}

/** The bar a running bot is still forming: outlined, never filled, with when it decides. */
export interface OverlayForming {
  readonly startMs: number;
  readonly closeMs: number;
  readonly label: string;
}

export interface StrategyChartOverlayState {
  /** The drawn candles' intervals, ascending — what an instant is placed against. */
  readonly bars: readonly OverlayBar[];
  /** Everything before this instant is shaded: the bars the bot never acted on. */
  readonly shadeBeforeMs: number | null;
  /** Backend prose naming the shaded region, drawn at its top-left. */
  readonly shadeLabel: string | null;
  /** Everything after this instant is shaded: the market after the run ended. */
  readonly shadeAfterMs: number | null;
  readonly forming: OverlayForming | null;
  readonly lines: readonly OverlayLine[];
  /** The selected candle's bar close. */
  readonly highlightCloseMs: number | null;
}

export const EMPTY_OVERLAY_STATE: StrategyChartOverlayState = Object.freeze({
  bars: [],
  shadeBeforeMs: null,
  shadeLabel: null,
  shadeAfterMs: null,
  forming: null,
  lines: [],
  highlightCloseMs: null,
});

const COLORS = {
  shade: 'rgba(7, 10, 17, 0.5)',
  shadeLabel: '#9598a1',
  start: '#7aa9ff',
  end: 'rgba(122, 169, 255, 0.7)',
  now: '#d1d4dc',
  forming: 'rgba(209, 212, 220, 0.55)',
  endLabel: '#9598a1',
  highlightFill: 'rgba(41, 98, 255, 0.08)',
  highlightEdge: 'rgba(122, 169, 255, 0.5)',
} as const;
const LABEL_FONT = '10px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';
const STRONG_LABEL_FONT = `600 ${LABEL_FONT}`;
/** Gap between a label and the line or edge it reads from. */
const LABEL_INSET = 4;

/**
 * Where an instant sits on the chart's logical (bar-index) axis.
 *
 * lightweight-charts draws candle `i` centred on logical `i`, so the candle
 * covers `[i - 0.5, i + 0.5]` and that span stands for its bar's interval: an
 * instant inside a bar lands proportionally inside its candle, a bar's close
 * on the candle's right edge. An instant in a gap between bars (a missing
 * bar, overnight) lands on the boundary between them; one beyond either end
 * extends at that end bar's length, at most one bar out, so a run stopped
 * hours after its last bar keeps its "Ended" line beside it. `null` when there
 * are no bars.
 */
export function logicalIndexAt(bars: readonly OverlayBar[], atMs: number): number | null {
  if (bars.length === 0) return null;
  const length = (bar: OverlayBar): number => Math.max(1, bar.closeMs - bar.startMs);
  const first = bars[0];
  if (atMs < first.startMs) return -0.5 - Math.min(1, (first.startMs - atMs) / length(first));
  let lo = 0;
  let hi = bars.length - 1;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (bars[mid].startMs <= atMs) lo = mid;
    else hi = mid - 1;
  }
  const bar = bars[lo];
  if (atMs <= bar.closeMs) return lo - 0.5 + (atMs - bar.startMs) / length(bar);
  if (lo === bars.length - 1) return lo + 0.5 + Math.min(1, (atMs - bar.closeMs) / length(bar));
  return lo + 0.5;
}

/** The part of a time scale a fractional bar index is placed with. */
export interface LogicalCoordinateScale {
  logicalToCoordinate(logical: Logical): number | null;
}

/** The x of a fractional bar index. The library answers only whole indices
 * (a fractional one comes back as 0), so a point between two bars is
 * interpolated between theirs. */
export function coordinateOfLogical(timeScale: LogicalCoordinateScale, logical: number): number | null {
  const whole = Math.floor(logical);
  const left = timeScale.logicalToCoordinate(whole as Logical);
  if (left === null || whole === logical) return left;
  const right = timeScale.logicalToCoordinate((whole + 1) as Logical);
  return right === null ? null : left + (right - left) * (logical - whole);
}

/**
 * The strategy chart's time-anchored decorations, which lightweight-charts
 * has no built-in for: the shaded regions before the bot started and after
 * it ended, a solid "Bot started" line, a dashed "Ended" line, a running
 * bot's "Now" line and the dashed outline of the bar it is forming (#2794),
 * and the selected candle's band.
 *
 * One instance per pane: the price pane's carries the labels, the others
 * repeat the shade and lines so a lower pane reads on the same clock.
 */
export class StrategyChartOverlay implements ISeriesPrimitive<Time> {
  private chart: IChartApiBase<Time> | null = null;
  private requestUpdate: (() => void) | null = null;
  private state: StrategyChartOverlayState = EMPTY_OVERLAY_STATE;
  private readonly views: readonly IPrimitivePaneView[];

  constructor(private readonly withLabels: boolean) {
    this.views = [
      { zOrder: () => 'bottom', renderer: () => ({ draw: (target) => this.drawBackground(target) }) },
      { zOrder: () => 'top', renderer: () => ({ draw: (target) => this.drawLines(target) }) },
    ];
  }

  attached(param: SeriesAttachedParameter<Time>): void {
    this.chart = param.chart;
    this.requestUpdate = param.requestUpdate;
  }

  detached(): void {
    this.chart = null;
    this.requestUpdate = null;
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return this.views;
  }

  /** What the overlay currently draws. */
  current(): StrategyChartOverlayState {
    return this.state;
  }

  update(state: StrategyChartOverlayState): void {
    this.state = state;
    this.requestUpdate?.();
  }

  private x(logical: number): number | null {
    const timeScale = this.chart?.timeScale();
    return timeScale === undefined ? null : coordinateOfLogical(timeScale, logical);
  }

  private xAt(atMs: number): number | null {
    const logical = logicalIndexAt(this.state.bars, atMs);
    return logical === null ? null : this.x(logical);
  }

  private drawBackground(target: RenderingTarget): void {
    const { shadeBeforeMs, shadeLabel, shadeAfterMs, highlightCloseMs, bars } = this.state;
    target.useMediaCoordinateSpace(({ context, mediaSize }) => {
      const shadeStart = shadeAfterMs === null ? null : this.xAt(shadeAfterMs);
      if (shadeStart !== null && shadeStart < mediaSize.width) {
        context.fillStyle = COLORS.shade;
        context.fillRect(Math.max(0, shadeStart), 0, mediaSize.width - Math.max(0, shadeStart), mediaSize.height);
      }
      const shadeEnd = shadeBeforeMs === null ? null : this.xAt(shadeBeforeMs);
      if (shadeEnd !== null && shadeEnd > 0) {
        const shadeWidth = Math.min(shadeEnd, mediaSize.width);
        context.fillStyle = COLORS.shade;
        context.fillRect(0, 0, shadeWidth, mediaSize.height);
        if (this.withLabels && shadeLabel !== null) {
          // Ends at the start line, whose own label reads rightwards from it;
          // left out when the shaded strip is too narrow to hold it.
          context.font = LABEL_FONT;
          if (context.measureText(shadeLabel).width + 2 * LABEL_INSET <= shadeWidth) {
            context.fillStyle = COLORS.shadeLabel;
            context.textAlign = 'right';
            context.fillText(shadeLabel, shadeWidth - LABEL_INSET, 12);
          }
        }
      }
      const index = highlightCloseMs === null ? -1 : bars.findIndex((bar) => bar.closeMs === highlightCloseMs);
      const left = index < 0 ? null : this.x(index - 0.5);
      const right = index < 0 ? null : this.x(index + 0.5);
      if (left !== null && right !== null) {
        context.fillStyle = COLORS.highlightFill;
        context.fillRect(left, 0, right - left, mediaSize.height);
        context.strokeStyle = COLORS.highlightEdge;
        context.lineWidth = 1;
        context.strokeRect(left + 0.5, 0.5, right - left - 1, mediaSize.height - 1);
      }
    });
  }

  private drawForming(context: CanvasRenderingContext2D, height: number): void {
    const forming = this.state.forming;
    const left = forming === null ? null : this.xAt(forming.startMs);
    const right = forming === null ? null : this.xAt(forming.closeMs);
    if (forming === null || left === null || right === null) return;
    context.strokeStyle = COLORS.forming;
    context.lineWidth = 1;
    context.setLineDash([4, 3]);
    context.strokeRect(left + 0.5, 0.5, right - left - 1, height - 1);
    if (!this.withLabels) return;
    // Read leftwards from the slot: right of it is the price scale.
    context.setLineDash([]);
    context.font = LABEL_FONT;
    context.fillStyle = COLORS.forming;
    context.textAlign = 'right';
    context.fillText(forming.label, left - LABEL_INSET, height - LABEL_INSET);
  }

  private drawLines(target: RenderingTarget): void {
    target.useMediaCoordinateSpace(({ context, mediaSize }) => {
      this.drawForming(context, mediaSize.height);
      for (const line of this.state.lines) {
        const x = this.xAt(line.atMs);
        if (x === null) continue;
        const start = line.emphasis === 'start';
        const now = line.emphasis === 'now';
        context.strokeStyle = start ? COLORS.start : now ? COLORS.now : COLORS.end;
        context.lineWidth = start ? 1.5 : 1;
        context.setLineDash(start ? [] : now ? [1, 2] : [3, 3]);
        context.beginPath();
        context.moveTo(x, 0);
        context.lineTo(x, mediaSize.height);
        context.stroke();
        if (!this.withLabels) continue;
        context.setLineDash([]);
        context.font = start ? STRONG_LABEL_FONT : LABEL_FONT;
        context.fillStyle = start ? COLORS.start : now ? COLORS.now : COLORS.endLabel;
        context.textAlign = start ? 'left' : 'right';
        context.fillText(line.label, start ? x + LABEL_INSET : x - LABEL_INSET, start ? 12 : now ? 38 : 25);
      }
      context.setLineDash([]);
    });
  }
}
