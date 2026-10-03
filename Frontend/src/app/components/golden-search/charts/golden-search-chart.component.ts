import { afterNextRender, ChangeDetectionStrategy, Component, DestroyRef, effect, ElementRef, inject, input, signal, untracked, viewChild } from '@angular/core';

import { highlightActions, type ChartSpec, type HighlightTarget } from './golden-search-chart-spec';
import { GOLDEN_SEARCH_CHART_THEME } from './golden-search-chart-theme';
import { GOLDEN_SEARCH_CHARTS, type ChartInstance, type ChartOption } from './golden-search-echarts';

const DRAW_FAILED = 'The chart could not be drawn.';
const SEE_TABLE = 'Every value is in the table below.';

/**
 * The one ECharts host every Golden Search chart uses (#2821). It loads the
 * library on first draw, draws the spec's option in the app's colours, keeps
 * the chart sized to its panel, and disposes it with the view. Hover detail
 * is the spec's tooltip; the summary sentence names the chart for assistive
 * technology, and "Show as table" offers every value without a mouse. It
 * draws what the spec holds and computes no number.
 */
@Component({
  selector: 'app-golden-search-chart',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-chart.component.html',
  styleUrl: './golden-search-chart.component.scss',
})
export class GoldenSearchChartComponent {
  readonly spec = input.required<ChartSpec>();
  /** Canvas height in pixels. */
  readonly height = input(320);
  /** Below this width the chart scrolls inside its panel instead of squeezing. */
  readonly minWidth = input(0);
  /** Offer "Show as table"; off for a chart whose values another chart's table already lists. */
  readonly offersTable = input(true);

  private readonly load = inject(GOLDEN_SEARCH_CHARTS);
  private readonly theme = inject(GOLDEN_SEARCH_CHART_THEME);
  private readonly element: HTMLElement = inject(ElementRef).nativeElement;
  private readonly canvas = viewChild.required<ElementRef<HTMLDivElement>>('canvas');
  private readonly chart = signal<ChartInstance | null>(null);
  private drawn: ChartOption | null = null;
  /** The walkthrough step's target, kept so a draw that lands later (or a redraw) shows it too. */
  private target: HighlightTarget | null = null;
  private observer: ResizeObserver | null = null;
  private destroyed = false;

  protected readonly showTable = signal(false);
  protected readonly failure = signal<string | null>(null);

  constructor() {
    afterNextRender(() => void this.start());
    effect(() => {
      const chart = this.chart();
      const spec = this.spec();
      // Only the chart and the spec trigger a draw, not signals a spec's option happens to read.
      if (chart !== null) untracked(() => this.draw(chart, spec));
    });
    inject(DestroyRef).onDestroy(() => {
      this.destroyed = true;
      this.observer?.disconnect();
      this.chart()?.dispose();
    });
  }

  /** Lights up a walkthrough step's target; null clears the last one. */
  highlight(target: HighlightTarget | null): void {
    this.target = target;
    const chart = this.chart();
    if (chart !== null && this.drawn !== null) this.showTarget(chart, this.drawn);
  }

  protected toggleTable(): void {
    this.showTable.update((shown) => !shown);
  }

  private async start(): Promise<void> {
    try {
      const library = await this.load();
      if (this.destroyed) return;
      const host = this.canvas().nativeElement;
      const chart = library.init(host);
      this.observer = new ResizeObserver(() => chart.resize());
      this.observer.observe(host);
      this.chart.set(chart);
    } catch (error: unknown) {
      this.fail(error);
    }
  }

  private draw(chart: ChartInstance, spec: ChartSpec): void {
    try {
      const option = spec.option(this.theme(this.element));
      const drawn = reducedMotion() ? { ...option, animation: false } : option;
      chart.draw(drawn);
      this.drawn = drawn;
      this.failure.set(null);
      if (this.target !== null) this.showTarget(chart, drawn);
    } catch (error: unknown) {
      // The last spec's drawing must not stand beside the failure as if it were this one's.
      chart.clear();
      this.fail(error);
    }
  }

  private showTarget(chart: ChartInstance, drawn: ChartOption): void {
    for (const action of highlightActions(drawn, this.target, this.spec().featured)) chart.dispatch(action);
  }

  /** A chart that cannot draw says so and opens its table, which holds every value. */
  private fail(error: unknown): void {
    this.drawn = null;
    const reason = error instanceof Error && error.message.length > 0 ? ` (${error.message})` : '';
    this.failure.set(this.offersTable() ? `${DRAW_FAILED} ${SEE_TABLE}${reason}` : `${DRAW_FAILED}${reason}`);
    this.showTable.set(true);
  }
}

/** The viewer asked for less motion. */
export function reducedMotion(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}
