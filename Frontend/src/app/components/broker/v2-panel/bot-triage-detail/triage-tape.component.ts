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

import { AssetIdentityComponent } from '../../../../shared/asset-identity/asset-identity.component';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import {
  CFG,
  computeScale,
  draw,
  type CandleRendererConfig,
} from '../gallery/lib/candle-renderer';
import type {
  ChartBar,
  ChartFillMarker,
  ChartLiveResolution,
} from '../lib/broker-v2-panel.types';

/** Coarsest first: the triage pane defaults to `1m` and only drops to `5s` on request. */
const RESOLUTIONS: readonly ChartLiveResolution[] = ['1m', '5s'];

/**
 * The triage pane's price tape.
 *
 * A presentational canvas over `gallery/lib/candle-renderer` — the same pure
 * renderer the gallery wall paints its tiles with, which that module was
 * written to be reused by. This component owns the DOM canvas, its resize
 * observer, and the interval control; the pane above it owns the fetch.
 *
 * Deliberately NOT `DualPaneChartComponent`: that one carries the live/Polygon
 * source switcher, the indicator rail, fullscreen, and a lightweight-charts
 * instance per mount — appropriate for the per-bot route that owns an SSE
 * stream, far too much for a pane that repaints every time an operator arrows
 * down the rail.
 *
 * It derives NO numbers from the bars — no last price, no session change. Those
 * are server-computed elsewhere (single numerical authority, CLAUDE.md #5);
 * this draws the candles it is handed and nothing else.
 */
@Component({
  selector: 'app-triage-tape',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent, TimestampDisplayComponent],
  templateUrl: './triage-tape.component.html',
  styleUrl: './triage-tape.component.scss',
})
export class TriageTapeComponent {
  readonly symbol = input.required<string>();
  readonly bars = input<readonly ChartBar[]>([]);
  readonly markers = input<readonly ChartFillMarker[]>([]);
  readonly loading = input(false);
  readonly failed = input(false);
  readonly resolution = input.required<ChartLiveResolution>();
  /** Server-computed last-bar time from the panel projection, never derived from `bars`. */
  readonly lastBarAtMs = input<number | null>(null);

  readonly resolutionChange = output<ChartLiveResolution>();
  readonly retry = output();

  protected readonly resolutions = RESOLUTIONS;

  private readonly destroyRef = inject(DestroyRef);
  private readonly chartStage = viewChild.required<ElementRef<HTMLDivElement>>('chartStage');
  private readonly chartCanvas = viewChild.required<ElementRef<HTMLCanvasElement>>('chartCanvas');

  /**
   * Seeded with the renderer default so the first paint — and any host where
   * `ResizeObserver` never fires, such as a jsdom test — still draws.
   */
  private readonly canvasSize = signal<{ width: number; height: number }>({
    width: CFG.width,
    height: CFG.height,
  });

  private readonly rendererCfg = computed<CandleRendererConfig>(() => ({
    ...CFG,
    ...this.canvasSize(),
  }));

  /**
   * A signal rather than a plain field, for the reason `BotTileComponent`
   * documents at length: `paint()` runs inside `effect(() => this.paint())`,
   * and a plain-field null check would return before reading any signal, so
   * the effect would register no dependencies and never re-run.
   */
  private readonly ctx = signal<CanvasRenderingContext2D | null>(null);

  protected readonly empty = computed(() => this.bars().length === 0);

  constructor() {
    effect(() => this.paint());
    afterNextRender(() => this.mountCanvas());
  }

  protected selectResolution(resolution: ChartLiveResolution): void {
    if (resolution === this.resolution()) return;
    this.resolutionChange.emit(resolution);
  }

  private mountCanvas(): void {
    const canvas = this.chartCanvas().nativeElement;

    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (!rect || rect.width <= 0 || rect.height <= 0) return;
      this.applyCanvasSize(canvas, rect.width, rect.height);
    });
    observer.observe(this.chartStage().nativeElement);
    this.destroyRef.onDestroy(() => observer.disconnect());

    this.ctx.set(canvas.getContext('2d'));
  }

  /** Sizes the backing store for `devicePixelRatio` while keeping the drawing API in CSS pixels. */
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
    draw(ctx, bars, this.markers(), computeScale(bars, cfg), null, cfg);
  }
}
