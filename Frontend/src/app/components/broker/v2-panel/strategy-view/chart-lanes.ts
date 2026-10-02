import { formatReceiptLabel } from '../../../../shared/pipes/receipt-label.pipe';
import { formatTimestampDisplay } from '../../../../shared/timestamp/timestamp-display';
import type {
  BotPanelView,
  ChartFillMarker,
  FeedContinuityView,
  StrategyViewResponse,
} from '../lib/broker-v2-panel.types';

type WorkingOrder = BotPanelView['working_orders'][number];
type FeedEvent = FeedContinuityView['events'][number];

/**
 * What the bot page knows about its run beyond the strategy view's own read
 * (#2794): the snapshot's clock while the bot runs, the owner's end, and the
 * facts the lanes under the chart draw. The Strategy Lab has none.
 */
export interface StrategyRunContext {
  /** The panel snapshot's backend clock while the bot runs; `null` once it stopped. */
  readonly nowMs: number | null;
  /** The owner's end while it is still ahead. */
  readonly scheduledEndAtMs: number | null;
  readonly fills: readonly ChartFillMarker[];
  readonly workingOrders: readonly WorkingOrder[];
  /** Market-data continuity, including the partial first minute a mid-minute join omits. */
  readonly feedEvents: readonly FeedEvent[];
}

export type LaneKey = 'decisions' | 'orders' | 'market' | 'bot';
export type LaneGlyph = 'up' | 'down' | 'dot' | 'ring' | 'span' | 'tick' | 'dashed';
export type LaneTone = 'bull' | 'bear' | 'neutral' | 'muted' | 'warn' | 'accent';

/** One event on a lane: an instant, or a span when `endMs` is set. */
export interface LaneMark {
  readonly key: string;
  readonly atMs: number;
  readonly endMs: number | null;
  readonly glyph: LaneGlyph;
  readonly tone: LaneTone;
  /** What it is and when, for its tooltip and screen readers. */
  readonly label: string;
}

export interface ChartLane {
  readonly key: LaneKey;
  readonly title: string;
  readonly marks: readonly LaneMark[];
}

function minuteOf(ms: number): string {
  return formatTimestampDisplay(ms, { mode: 'local', granularity: 'minute' });
}

/** The forming bar while the bot runs: the bar after the last closed one, and when it decides. */
export function formingBar(
  view: StrategyViewResponse,
  nowMs: number | null,
): { readonly startMs: number; readonly closeMs: number } | null {
  const last = view.candles.at(-1);
  if (nowMs === null || last === undefined) return null;
  const startMs = last.bar_close_ms;
  const closeMs = startMs + view.decision_timeframe_ms;
  return nowMs >= startMs && nowMs < closeMs ? { startMs, closeMs } : null;
}

function decisionLane(view: StrategyViewResponse): ChartLane {
  const marks = view.candles.map((candle): LaneMark => {
    const at = minuteOf(candle.bar_close_ms);
    if (candle.phase === 'before_start') {
      return {
        key: `bar:${candle.bar_close_ms}`, atMs: candle.bar_close_ms, endMs: null, glyph: 'ring', tone: 'muted',
        label: `${at} · ${candle.phase_text ?? 'Before start'}`,
      };
    }
    const signal = candle.explanation.signal;
    return {
      key: `bar:${candle.bar_close_ms}`,
      atMs: candle.bar_close_ms,
      endMs: null,
      glyph: signal === 'ENTER' ? 'up' : signal === 'EXIT' ? 'down' : 'dot',
      tone: signal === 'ENTER' ? 'bull' : signal === 'EXIT' ? 'bear' : 'neutral',
      label: `${at} · ${formatReceiptLabel(signal)}`,
    };
  });
  return { key: 'decisions', title: 'Decisions', marks };
}

function orderLane(context: StrategyRunContext): ChartLane {
  const fills = context.fills.map((fill): LaneMark => ({
    key: `fill:${fill.event_key}`,
    atMs: fill.filled_at_ms,
    endMs: null,
    glyph: fill.side === 'buy' ? 'up' : 'down',
    tone: fill.side === 'buy' ? 'bull' : 'bear',
    label: `${minuteOf(fill.filled_at_ms)} · ${fill.side === 'buy' ? 'Bought' : 'Sold'} ${fill.quantity} @ ${fill.price}`,
  }));
  const working = context.workingOrders.map((order): LaneMark => ({
    key: `order:${order.order_ref}`,
    atMs: order.observed_at_ms,
    endMs: null,
    glyph: 'ring',
    tone: 'accent',
    label: `${minuteOf(order.observed_at_ms)} · Working ${order.side} ${order.quantity ?? ''} ${order.symbol} · `
      + formatReceiptLabel(order.status),
  }));
  return { key: 'orders', title: 'Orders', marks: [...fills, ...working] };
}

function marketLane(context: StrategyRunContext): ChartLane {
  const marks = context.feedEvents.map((event): LaneMark => {
    const span = event.window_start_ms !== null && event.window_end_ms !== null;
    const trouble = event.kind !== 'recovered';
    const duration = event.duration_label === null ? '' : ` · ${event.duration_label}`;
    return {
      key: `feed:${event.evidence_seq}`,
      atMs: span ? (event.window_start_ms ?? event.occurred_at_ms) : event.occurred_at_ms,
      endMs: span ? event.window_end_ms : null,
      glyph: span ? 'span' : trouble ? 'tick' : 'dot',
      tone: trouble ? 'warn' : 'neutral',
      label: `${minuteOf(event.occurred_at_ms)} · ${event.label}${duration}`,
    };
  });
  return { key: 'market', title: 'Market data', marks };
}

function botLane(view: StrategyViewResponse, context: StrategyRunContext): ChartLane {
  const marks: LaneMark[] = [];
  const started = view.run_started_at_ms ?? null;
  const stopped = view.run_stopped_at_ms ?? null;
  if (started !== null) {
    marks.push({
      key: 'started', atMs: started, endMs: null, glyph: 'tick', tone: 'accent', label: `Started ${minuteOf(started)}`,
    });
  }
  if (stopped !== null) {
    marks.push({
      key: 'stopped', atMs: stopped, endMs: null, glyph: 'tick', tone: 'neutral', label: `Ended ${minuteOf(stopped)}`,
    });
  }
  const forming = formingBar(view, context.nowMs);
  if (forming !== null) {
    marks.push({
      key: 'next-decision', atMs: forming.closeMs, endMs: null, glyph: 'dashed', tone: 'accent',
      label: `Next decision ${minuteOf(forming.closeMs)}`,
    });
  }
  if (stopped === null && context.scheduledEndAtMs !== null) {
    marks.push({
      key: 'ends', atMs: context.scheduledEndAtMs, endMs: null, glyph: 'dashed', tone: 'neutral',
      label: `Ends ${minuteOf(context.scheduledEndAtMs)}`,
    });
  }
  return { key: 'bot', title: 'Bot', marks };
}

/**
 * The four lanes under the strategy chart (#2794 R4), on the chart's clock:
 * the bot's decisions and before-start evaluations, its fills and working
 * orders, market-data continuity (gaps -- the partial first minute a
 * mid-minute join omits among them -- and recoveries) and the run's start,
 * end and next decision. Every word that is not a time comes from the
 * backend or its receipt labels.
 */
export function chartLanes(view: StrategyViewResponse, context: StrategyRunContext): readonly ChartLane[] {
  return [decisionLane(view), orderLane(context), marketLane(context), botLane(view, context)];
}
