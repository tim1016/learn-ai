import { ChangeDetectionStrategy, Component, computed, contentChild, effect, ElementRef, inject, input, signal, untracked, viewChild } from '@angular/core';

import { MarkdownDrawerService } from '../../../shared/markdown-drawer/markdown-drawer.service';
import { GoldenSearchChartComponent } from './golden-search-chart.component';
import { CHART_GUIDES, type GoldenSearchChartId } from './golden-search-chart-guides';
import { GoldenSearchWalkthroughComponent } from './golden-search-walkthrough.component';

/**
 * A chart panel (#2821): the chart's title and the question it answers, the
 * projected chart, legend (`[legend]`) and caption (`[caption]`), and two
 * ways to learn to read it — "About this chart" opens its guide section in
 * the drawer beside the live chart, and "Walk me through it" steps through
 * the guide's reading steps, lighting up each one on the chart. Its span on
 * the page grid is set where it is placed (`data-span`).
 */
@Component({
  selector: 'app-golden-search-panel',
  imports: [GoldenSearchWalkthroughComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-panel.component.html',
  styleUrl: './golden-search-panel.component.scss',
  host: { role: 'region', '[attr.aria-labelledby]': 'headingId()' },
})
export class GoldenSearchPanelComponent {
  readonly chart = input.required<GoldenSearchChartId>();
  /** Tells apart two panels of one chart on a page (the all-period search's and the recent fit's): it suffixes the title and the heading's id. */
  readonly instance = input<string | null>(null);

  private readonly drawer = inject(MarkdownDrawerService);
  private readonly host = contentChild(GoldenSearchChartComponent);
  private readonly walkButton = viewChild.required<ElementRef<HTMLButtonElement>>('walk');
  private readonly heading = viewChild.required<ElementRef<HTMLHeadingElement>>('heading');

  protected readonly guide = computed(() => CHART_GUIDES[this.chart()]);
  protected readonly title = computed(() => {
    const instance = this.instance();
    return instance === null ? this.guide().title : `${this.guide().title} · ${instance}`;
  });
  protected readonly headingId = computed(() => {
    const instance = this.instance();
    return instance === null ? `gs-chart-${this.chart()}` : `gs-chart-${this.chart()}-${instance.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`;
  });
  protected readonly walking = signal(false);
  /** The walkthrough's current step; null when no walkthrough is open. */
  private readonly step = signal<number | null>(null);

  constructor() {
    // The chart may appear after the walkthrough opened (its runs still loading): it gets the current step when it does.
    effect(() => {
      const host = this.host();
      const step = this.step();
      if (host !== undefined) untracked(() => host.highlight(step === null ? null : this.guide().steps[step].target));
    });
  }

  /** Moves focus to the panel's title, which brings the panel into view (a summary row's "See the evidence"). */
  focusHeading(): void {
    this.heading().nativeElement.focus();
  }

  protected about(): void {
    this.drawer.open('golden-search-guide', this.chart());
  }

  protected toggleWalk(): void {
    if (this.walking()) this.endWalk();
    else this.walking.set(true);
  }

  protected showStep(index: number): void {
    this.step.set(index);
  }

  protected endWalk(): void {
    this.step.set(null);
    this.walking.set(false);
    this.walkButton().nativeElement.focus();
  }
}
