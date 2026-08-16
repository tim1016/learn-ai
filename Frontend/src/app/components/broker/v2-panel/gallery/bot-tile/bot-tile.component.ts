import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  afterNextRender,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';
import { Router } from '@angular/router';
import { TickMarkType } from 'lightweight-charts';
import type { ChartBar, ChartFillMarker, GalleryBotView } from '../lib/gallery.types';
import {
  CFG,
  barIndexAtX,
  computeScale,
  draw,
  type CandleRendererConfig,
} from '../lib/candle-renderer';
import { toCandle } from '../../lib/chart-bar-mapping';
import { fmtInteger, fmtNumber, fmtSignedCurrency, fmtSignedNumber } from '../../../format';
import { formatChartAxisTick } from '../../../../../shared/charts/chart-utils';
import { AssetIdentityComponent } from '../../../../../shared/asset-identity';

export type BotStatusTone = 'bull' | 'warn' | 'muted';
type SignTone = 'positive' | 'negative' | 'neutral';

/**
 * Pure status-ring colour derivation (design spec §6, D4): running &
 * healthy → bull green, running & needing attention → warn amber, not
 * running (stopped/off-duty) → muted grey. Kept standalone/exported so the
 * mapping is unit-testable without rendering the component.
 */
export function botStatusTone(bot: Pick<GalleryBotView, 'running' | 'needs_attention'>): BotStatusTone {
  if (!bot.running) return 'muted';
  return bot.needs_attention ? 'warn' : 'bull';
}

function signTone(value: number | null): SignTone {
  if (value === null || value === 0) return 'neutral';
  return value > 0 ? 'positive' : 'negative';
}

/** First fill in `markers` landing inside `bar`'s `[start_ms, end_ms)` window, formatted `SIDE qty @ price`. */
function fillTextForBar(bar: ChartBar, markers: readonly ChartFillMarker[]): string | null {
  const match = markers.find((m) => m.filled_at_ms >= bar.start_ms && m.filled_at_ms < bar.end_ms);
  return match ? `${match.side.toUpperCase()} ${match.quantity} @ ${match.price}` : null;
}

/**
 * One live bot's gallery tile: header identity/status ring, a custom-canvas
 * candle chart with a DOM legend, and a single guarded quick action.
 *
 * The chart is painted on a plain `<canvas>` via `lib/candle-renderer.ts`
 * (pure geometry/draw functions — this component owns the DOM, the
 * `ResizeObserver`, and pointer events only, per design spec §3.2/§3.3). No
 * per-tick Angular change detection: bars/markers ticking in are read
 * inside an `effect()` that calls `computeScale` + `draw` directly, the
 * same zoneless discipline the previous lightweight-charts mount used
 * (`series.setData()` swapped for a canvas repaint).
 */
@Component({
  selector: 'app-bot-tile',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent],
  host: {
    '(document:keydown.escape)': 'onEscape()',
  },
  templateUrl: './bot-tile.component.html',
  styleUrl: './bot-tile.component.scss',
})
export class BotTileComponent {
  readonly bot = input.required<GalleryBotView>();
  readonly bars = input.required<readonly ChartBar[]>();
  readonly markers = input<readonly ChartFillMarker[]>([]);
  readonly broker = input.required<string>();
  readonly accountId = input.required<string>();
  /** Set by the dock while this tile's confirmed quick action is in flight — disables the button and marks it `aria-busy` without restyling the rest of the tile. */
  readonly pending = input<boolean>(false);

  readonly action = output<{ sid: string; actionId: string }>();

  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);
  private readonly chartContainer =
    viewChild.required<ElementRef<HTMLDivElement>>('chartContainer');
  private readonly chartCanvas = viewChild.required<ElementRef<HTMLCanvasElement>>('chartCanvas');
  private readonly confirmCancelButton =
    viewChild<ElementRef<HTMLButtonElement>>('confirmCancelButton');
  private readonly actionButton = viewChild<ElementRef<HTMLButtonElement>>('actionButton');

  protected readonly confirmOpen = signal(false);
  /** Hovered bar index for the crosshair + legend swap; `null` when the pointer is off the chart. */
  protected readonly hoverIndex = signal<number | null>(null);
  /** The canvas's last applied CSS-pixel size — seeded with the renderer default so the very first paint (and any host where `ResizeObserver` never fires, e.g. tests) still draws at a sane size. */
  private readonly canvasSize = signal<{ width: number; height: number }>({
    width: CFG.width,
    height: CFG.height,
  });

  protected readonly statusTone = computed(() => botStatusTone(this.bot()));

  private readonly rendererCfg = computed<CandleRendererConfig>(() => ({
    ...CFG,
    ...this.canvasSize(),
  }));

  /** Session return is server-computed (`GalleryBotView.session_change_pct`), scoped to TODAY's session — never re-derive this from `bars[0]`, which can be a prior session's tail bar (single numerical authority; CLAUDE.md #5, same reasoning as `dayPnl` below). */
  private readonly deltaPct = computed<number | null>(() => this.bot().session_change_pct);
  protected readonly formattedDeltaPct = computed<string | null>(() => {
    const delta = this.deltaPct();
    return delta === null ? null : `${fmtSignedNumber(delta * 100, 2)}%`;
  });
  protected readonly deltaTone = computed<SignTone>(() => signTone(this.deltaPct()));

  protected readonly formattedFills = computed(() => fmtInteger(this.bot().fills_today));

  /** Day P&L is server-computed (`GalleryBotView.day_pnl`) — the tile formats it, never re-derives it (single P&L authority; CLAUDE.md #5). */
  private readonly dayPnl = computed<number | null>(() => this.bot().day_pnl);
  protected readonly formattedPnl = computed(() => fmtSignedCurrency(this.dayPnl()));
  protected readonly pnlTone = computed<SignTone>(() => signTone(this.dayPnl()));

  private readonly hoverBar = computed<ChartBar | null>(() => {
    const idx = this.hoverIndex();
    if (idx === null) return null;
    const bars = this.bars();
    return idx >= 0 && idx < bars.length ? bars[idx] : null;
  });
  /** `HH:MM O H L C V [SIDE qty @ price]` for the hovered bar; `null` reverts the legend to the default Δ%/fills/P&L readout. */
  protected readonly hoverLegend = computed<string | null>(() => {
    const bar = this.hoverBar();
    if (!bar) return null;
    const candle = toCandle(bar);
    const time = formatChartAxisTick(candle.time, undefined, TickMarkType.Time);
    const ohlcv = `${time} O${fmtNumber(candle.open)} H${fmtNumber(candle.high)} L${fmtNumber(candle.low)} C${fmtNumber(candle.close)} V${fmtInteger(bar.volume)}`;
    const fill = fillTextForBar(bar, this.markers());
    return fill ? `${ohlcv} ${fill}` : ohlcv;
  });

  protected readonly actionTitle = computed(() => {
    const primaryAction = this.bot().primary_action;
    return !primaryAction.enabled && primaryAction.disabled_reason
      ? primaryAction.disabled_reason
      : primaryAction.label;
  });
  protected readonly actionAriaLabel = computed(() => {
    const primaryAction = this.bot().primary_action;
    return !primaryAction.enabled && primaryAction.disabled_reason
      ? `${primaryAction.label} — ${primaryAction.disabled_reason}`
      : primaryAction.label;
  });
  protected readonly actionIsStop = computed(() =>
    this.bot().primary_action.action_id !== 'resume',
  );
  protected readonly confirmText = computed(() => {
    const view = this.bot();
    return `${view.primary_action.label} ${view.symbol} · ${view.sid}?`;
  });
  /**
   * The status ring's colour is not the only `needs_attention` signal (spec
   * §8) — running-healthy and running-needing-attention both show the same
   * "Stop" action, so colour alone would be the only differentiator between
   * them. Folding a text hint into the chart region's existing aria-label
   * (rather than inventing a separate ARIA construct on the decorative
   * ring) keeps it on an element already in the accessibility tree.
   */
  protected readonly chartAriaLabel = computed(() => {
    const view = this.bot();
    const attentionSuffix = view.needs_attention ? ' — needs attention' : '';
    return `Open ${view.symbol} · ${view.sid} detail${attentionSuffix}`;
  });

  /**
   * A signal, not a plain field: `paint()` (below) is only invoked from
   * `effect(() => this.paint())`, and on that effect's very first run —
   * before `afterNextRender`'s `mountCanvas()` has fired — a plain-field
   * `ctx` would still be `null`. `paint()` would bail out on the null check
   * *before* reading `bars()`/`markers()`/etc., so the effect would finish
   * that run having read zero signals and register no dependencies — it
   * would then never re-run on its own for the lifetime of the component,
   * since a signal effect only re-fires when a signal it actually read
   * changes. Making `ctx` itself a signal means the effect's null check
   * reads a tracked signal too, so `mountCanvas()` setting it (null →
   * real context) is itself a dependency change that re-fires the effect —
   * at which point it proceeds past the check and reads the rest.
   */
  private readonly ctx = signal<CanvasRenderingContext2D | null>(null);

  constructor() {
    effect(() => this.paint());
    effect(() => {
      // Move keyboard focus into the confirm when it opens, mirroring
      // `TypedHaltConfirmComponent` — a wall of tiles with no focus
      // management would strand keyboard/screen-reader operators on the
      // toolbar behind the confirm for a live Stop/Resume control.
      if (this.confirmOpen()) {
        queueMicrotask(() => this.confirmCancelButton()?.nativeElement.focus());
      }
    });
    afterNextRender(() => this.mountCanvas());
  }

  protected onBodyClick(): void {
    void this.router.navigate([
      '/brokers', this.broker(), 'accounts', this.accountId(), 'bots', this.bot().sid,
    ]);
  }

  protected onBodySpaceKey(event: Event): void {
    // Space defaults to page-scroll on a focusable div; suppress that
    // since Space here activates navigation instead.
    event.preventDefault();
    this.onBodyClick();
  }

  protected onChartHover(event: MouseEvent): void {
    const bars = this.bars();
    if (bars.length === 0) return;
    const rect = this.chartContainer().nativeElement.getBoundingClientRect();
    const scale = computeScale(bars, this.rendererCfg());
    this.hoverIndex.set(barIndexAtX(event.clientX - rect.left, scale, bars.length));
  }

  protected onChartLeave(): void {
    this.hoverIndex.set(null);
  }

  protected onActionClick(): void {
    if (!this.bot().primary_action.enabled) return;
    this.confirmOpen.set(true);
  }

  protected confirmAction(): void {
    const view = this.bot();
    this.confirmOpen.set(false);
    this.action.emit({ sid: view.sid, actionId: view.primary_action.action_id });
  }

  protected cancelAction(): void {
    this.confirmOpen.set(false);
    // The Cancel button that currently holds focus is about to leave the
    // DOM (the confirm block is `@if`-gated) — without this, a keyboard/AT
    // operator's focus falls back to `<body>`, stranding them at the top of
    // the page instead of back on the control they were just using.
    queueMicrotask(() => this.actionButton()?.nativeElement.focus());
  }

  protected onEscape(): void {
    if (this.confirmOpen()) {
      this.cancelAction();
    }
  }

  private mountCanvas(): void {
    const canvas = this.chartCanvas().nativeElement;

    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (!rect || rect.width <= 0 || rect.height <= 0) return;
      this.applyCanvasSize(canvas, rect.width, rect.height);
    });
    observer.observe(this.chartContainer().nativeElement);
    this.destroyRef.onDestroy(() => observer.disconnect());

    // Setting the signal (not assigning a plain field) is what re-arms the
    // paint effect — see the `ctx` field doc.
    this.ctx.set(canvas.getContext('2d'));
  }

  /** Sizes the canvas's backing store for `devicePixelRatio` (crisp on HiDPI) while keeping the drawing API in CSS-pixel coordinates via `setTransform`. */
  private applyCanvasSize(canvas: HTMLCanvasElement, width: number, height: number): void {
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    this.ctx()?.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.canvasSize.set({ width, height });
  }

  private paint(): void {
    const ctx = this.ctx();
    if (!ctx) return;
    const bars = this.bars();
    const cfg = this.rendererCfg();
    const scale = computeScale(bars, cfg);
    draw(ctx, bars, this.markers(), scale, this.hoverIndex(), cfg);
  }
}
