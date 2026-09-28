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
  signal,
  viewChild,
} from '@angular/core';

import {
  CFG,
  computeScale,
  draw,
  type CandleRendererConfig,
} from '../../broker/v2-panel/gallery/lib/candle-renderer';
import type { ChartBar, ChartFillMarker } from '../../broker/v2-panel/gallery/lib/gallery.types';

/**
 * Today's candles for one Wall tile, with the bot's fills, from the gallery
 * live feed (PRD #2560 D11). Painted on a canvas by the gallery's candle
 * renderer; a `ResizeObserver` keeps the backing store sized to the tile.
 * Decorative for assistive technology: the tile names the bot and its facts
 * in text.
 */
@Component({
  selector: 'app-home-sparkline',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div #container class="home-sparkline">
      <canvas #canvas aria-hidden="true"></canvas>
      @if (bars().length === 0) {
        <span class="home-sparkline__empty">No bars yet today</span>
      }
    </div>
  `,
  styles: [`
    :host { display: block; }
    .home-sparkline { position: relative; height: 5.5rem; }
    canvas { display: block; width: 100%; height: 100%; }
    .home-sparkline__empty {
      position: absolute; inset: 0; display: grid; place-items: center;
      color: var(--text-secondary); font-size: var(--fs-xs);
    }
  `],
})
export class HomeSparklineComponent {
  readonly bars = input.required<readonly ChartBar[]>();
  readonly markers = input<readonly ChartFillMarker[]>([]);

  private readonly destroyRef = inject(DestroyRef);
  private readonly container = viewChild.required<ElementRef<HTMLDivElement>>('container');
  private readonly canvas = viewChild.required<ElementRef<HTMLCanvasElement>>('canvas');
  private readonly size = signal({ width: CFG.width, height: CFG.height });
  /** A signal so the paint effect re-runs once the canvas has a context. */
  private readonly ctx = signal<CanvasRenderingContext2D | null>(null);
  private readonly config = computed<CandleRendererConfig>(() => ({
    ...CFG,
    ...this.size(),
    showLastPriceTag: false,
  }));

  constructor() {
    effect(() => {
      const ctx = this.ctx();
      if (ctx === null) return;
      const bars = this.bars();
      const config = this.config();
      draw(ctx, bars, this.markers(), computeScale(bars, config), null, config);
    });
    afterNextRender(() => {
      const canvas = this.canvas().nativeElement;
      const observer = new ResizeObserver((entries) => {
        const rect = entries[0]?.contentRect;
        if (!rect || rect.width <= 0 || rect.height <= 0) return;
        const ratio = window.devicePixelRatio || 1;
        canvas.width = Math.round(rect.width * ratio);
        canvas.height = Math.round(rect.height * ratio);
        this.ctx()?.setTransform(ratio, 0, 0, ratio, 0, 0);
        this.size.set({ width: rect.width, height: rect.height });
      });
      observer.observe(this.container().nativeElement);
      this.destroyRef.onDestroy(() => observer.disconnect());
      this.ctx.set(canvas.getContext('2d'));
    });
  }
}
