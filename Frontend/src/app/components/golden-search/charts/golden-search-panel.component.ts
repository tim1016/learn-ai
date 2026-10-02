import { ChangeDetectionStrategy, Component, computed, contentChild, ElementRef, inject, input, signal, viewChild } from '@angular/core';

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

  private readonly drawer = inject(MarkdownDrawerService);
  private readonly host = contentChild(GoldenSearchChartComponent);
  private readonly walkButton = viewChild.required<ElementRef<HTMLButtonElement>>('walk');

  protected readonly guide = computed(() => CHART_GUIDES[this.chart()]);
  protected readonly headingId = computed(() => `gs-chart-${this.chart()}`);
  protected readonly walking = signal(false);

  protected about(): void {
    this.drawer.open('golden-search-guide', this.chart());
  }

  protected toggleWalk(): void {
    if (this.walking()) this.endWalk();
    else this.walking.set(true);
  }

  protected showStep(index: number): void {
    this.host()?.highlight(this.guide().steps[index].target);
  }

  protected endWalk(): void {
    this.host()?.highlight(null);
    this.walking.set(false);
    this.walkButton().nativeElement.focus();
  }
}
