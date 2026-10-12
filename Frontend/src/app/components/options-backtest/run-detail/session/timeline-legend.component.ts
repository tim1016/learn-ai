import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { AssetIdentityComponent } from '../../../../shared/asset-identity';
import { OptionContractComponent } from '../../../../shared/option-contract/option-contract.component';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { CHART_SERIES_COLOR_TOKENS } from '../../../../shared/trading-chart/chart-series-color-tokens';
import type { LegendEntry, NotDrawn, PanePlan, TimelinePlan } from '../timeline-plan';
import { contractOf } from '../words';

interface PaneView extends PanePlan {
  readonly entries: readonly (LegendEntry & { readonly css: string })[];
}

/** The token's colour as the stylesheet names it, for a swatch drawn in markup. The chart reads the same token at its own boundary. */
function cssColour(entry: LegendEntry): string {
  const definition = CHART_SERIES_COLOR_TOKENS.get(entry.token);
  return definition === undefined ? 'currentColor' : `var(${definition.cssVar})`;
}

/**
 * What each pane of the chart holds, top to bottom, in words: every line and mark with the way it is drawn, so that colour
 * is never the only sign. Under it, what is not drawn and why: for a series the server withheld its own words, for what
 * the page could not place its own sentence.
 */
@Component({
  selector: 'app-options-backtest-timeline-legend',
  imports: [AssetIdentityComponent, OptionContractComponent, ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './timeline-legend.component.html',
  styleUrl: './timeline-legend.component.scss',
})
export class TimelineLegendComponent {
  readonly plan = input.required<TimelinePlan>();

  protected readonly panes = computed<readonly PaneView[]>(() =>
    this.plan().panes.map((pane) => ({ ...pane, entries: pane.entries.map((entry) => ({ ...entry, css: cssColour(entry) })) })),
  );
  protected readonly missing = computed<readonly NotDrawn[]>(() => this.plan().notDrawn);

  protected contractIn(item: { readonly contract: PanePlan['contract'] }) {
    return item.contract === null ? null : contractOf(item.contract);
  }
}
