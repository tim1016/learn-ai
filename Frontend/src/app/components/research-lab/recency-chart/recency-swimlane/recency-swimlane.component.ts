import { ChangeDetectionStrategy, Component, computed, input, output } from "@angular/core";
import {
  computeSwimlaneLayout,
  type RecencySwimlaneTrade,
  type SwimlaneBarLayout,
} from "./recency-swimlane-layout";

const BAR_HEIGHT = 15;
const SUB_ROW_GAP = 4;
const LANE_GAP = 16;
const LABEL_WIDTH = 62;
const RIGHT_MARGIN = 18;
const TOP_PADDING = 14;
const CAP_WIDTH = 3.5;
const VIEW_BOX_WIDTH = 900;
const GRAPH_WIDTH = VIEW_BOX_WIDTH - LABEL_WIDTH - RIGHT_MARGIN;

const LABEL_MIN_WIDTH = 60;

export interface RenderedBar {
  trade: RecencySwimlaneTrade;
  x: number;
  y: number;
  width: number;
  height: number;
  capWidth: number;
  fillColor: string;
  opacity: number;
  ariaLabel: string;
  durationLabel: string;
  showLabel: boolean;
}

export interface RenderedLane {
  symbol: string;
  y: number;
  height: number;
}

function durationLabelFor(trade: RecencySwimlaneTrade): string {
  return `${trade.holdingSessions} session${trade.holdingSessions === 1 ? "" : "s"}`;
}

/**
 * Per-symbol trade swimlane (design spec §5.1, D6-D10): lanes sorted by
 * recency, sub-row-packed bars (never nudged along the time axis), fill
 * = PnL diverging color, opacity = Sharpe. This is the tracer-bullet
 * render — zoom/pan, the sticky ruler, display modes, combo fold/unfold,
 * and the focus panel land in later slices; this component's contract is
 * "given a flat trade projection, render the lanes and let the caller
 * react to a selection."
 */
@Component({
  selector: "app-recency-swimlane",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./recency-swimlane.component.html",
  styleUrls: ["./recency-swimlane.component.scss"],
})
export class RecencySwimlaneComponent {
  readonly trades = input<RecencySwimlaneTrade[]>([]);
  readonly windowStartMs = input<number | null>(null);
  readonly windowEndMs = input<number | null>(null);

  readonly tradeSelected = output<RecencySwimlaneTrade>();

  private readonly effectiveWindow = computed<{ start: number; end: number }>(() => {
    const explicitStart = this.windowStartMs();
    const explicitEnd = this.windowEndMs();
    if (explicitStart !== null && explicitEnd !== null) {
      return { start: explicitStart, end: explicitEnd };
    }
    const trades = this.trades();
    if (trades.length === 0) return { start: 0, end: 1 };
    const start = trades.reduce((min, t) => Math.min(min, t.entryMs), trades[0].entryMs);
    const endRaw = trades.reduce((max, t) => Math.max(max, t.exitMs), trades[0].exitMs);
    return { start, end: endRaw > start ? endRaw : start + 1 };
  });

  /**
   * Virtualization (design spec D13): only trades whose span overlaps the
   * visible window are laid out at all — a trade entirely before or after
   * the window contributes nothing (not even to the PnL color domain or
   * sub-row packing of the trades that ARE shown). A trade straddling the
   * edge is display-clipped by xAt()'s own clamping, not dropped here.
   */
  private readonly visibleTrades = computed<RecencySwimlaneTrade[]>(() => {
    const { start, end } = this.effectiveWindow();
    return this.trades().filter((t) => t.exitMs >= start && t.entryMs <= end);
  });

  private readonly layout = computed(() => computeSwimlaneLayout(this.visibleTrades()));

  private xAt(ms: number): number {
    const { start, end } = this.effectiveWindow();
    const span = end - start || 1;
    const clamped = Math.max(start, Math.min(end, ms));
    return LABEL_WIDTH + ((clamped - start) / span) * GRAPH_WIDTH;
  }

  readonly lanes = computed<RenderedLane[]>(() => {
    let y = TOP_PADDING;
    const result: RenderedLane[] = [];
    for (const lane of this.layout().lanes) {
      const height = lane.subRowCount * (BAR_HEIGHT + SUB_ROW_GAP);
      result.push({ symbol: lane.symbol, y, height });
      y += height + LANE_GAP;
    }
    return result;
  });

  readonly viewBoxHeight = computed(() => {
    const lanes = this.lanes();
    if (lanes.length === 0) return TOP_PADDING * 2;
    const last = lanes[lanes.length - 1];
    return last.y + last.height + TOP_PADDING;
  });

  readonly bars = computed<RenderedBar[]>(() => {
    const laneY = new Map(this.lanes().map((l) => [l.symbol, l.y]));
    return this.layout().bars.map((bar): RenderedBar => this.toRenderedBar(bar, laneY.get(bar.trade.symbol) ?? TOP_PADDING));
  });

  private toRenderedBar(bar: SwimlaneBarLayout, laneTop: number): RenderedBar {
    const x1 = this.xAt(bar.trade.entryMs);
    const x2 = this.xAt(bar.trade.exitMs);
    const width = Math.max(3, x2 - x1);
    const y = laneTop + bar.subRow * (BAR_HEIGHT + SUB_ROW_GAP);
    return {
      trade: bar.trade,
      x: x1,
      y,
      width,
      height: BAR_HEIGHT,
      capWidth: Math.min(CAP_WIDTH, width),
      fillColor: bar.fillColor,
      opacity: bar.opacity,
      ariaLabel: this.ariaLabelFor(bar),
      durationLabel: durationLabelFor(bar.trade),
      showLabel: width >= LABEL_MIN_WIDTH,
    };
  }

  private ariaLabelFor(bar: SwimlaneBarLayout): string {
    const t = bar.trade;
    const sign = t.pnl >= 0 ? "+" : "";
    return `${t.symbol} ${t.strategyKey}, ${sign}${t.pnl.toFixed(2)} PnL, ${durationLabelFor(t)}`;
  }

  select(trade: RecencySwimlaneTrade): void {
    this.tradeSelected.emit(trade);
  }

  onKeydown(event: KeyboardEvent, trade: RecencySwimlaneTrade): void {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      this.select(trade);
    }
  }
}
