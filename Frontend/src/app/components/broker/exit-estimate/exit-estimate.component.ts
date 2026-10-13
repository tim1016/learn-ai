import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { OptionContractComponent } from '../../../shared/option-contract/option-contract.component';
import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { ageSeconds } from '../broker-deploy-page/option-structure/option-preview-display';
import type { ExitEstimateView, ExitLegQuoteView, FigureView } from '../v2-panel/lib/broker-v2-panel.service';

type FigureKey = 'net_close' | 'gross_buy_back';

interface FigureRow {
  readonly key: FigureKey;
  readonly label: string;
  readonly figure: FigureView;
  readonly terms: NonNullable<FigureView['terms']>;
}

interface LegRow {
  readonly leg: ExitLegQuoteView;
  readonly side: 'bought' | 'sold';
  readonly count: number;
  readonly ageSeconds: number | null;
  /** "no bid", "no ask" or "no bid or ask": the sides that have not arrived, or `null` when both have. */
  readonly missing: string | null;
}

let nextHeadingId = 0;

/**
 * What getting out of a held option position costs at the quotes held (#3100).
 *
 * Purely presentational. Python authors both figures with their terms, source
 * and age, how far each leg's quote can be trusted, and the lines about what
 * is not settled; this card lists them in that order, says a stale or unknown
 * figure is one, and compares nothing. An unknown figure has no amount here,
 * never "$0.00", and its source is the reason. It informs; nothing on the page
 * is blocked by it.
 */
@Component({
  selector: 'app-exit-estimate',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AuthoredUsdPipe, OptionContractComponent, TimestampDisplayComponent],
  templateUrl: './exit-estimate.component.html',
  styleUrl: './exit-estimate.component.scss',
})
export class ExitEstimateComponent {
  readonly estimate = input.required<ExitEstimateView>();

  protected readonly headingId = `exit-estimate-heading-${nextHeadingId++}`;

  protected readonly figures = computed<readonly FigureRow[]>(() => {
    const estimate = this.estimate();
    const rows: readonly [FigureKey, string, FigureView][] = [
      ['net_close', 'Closing everything as one order', estimate.net_close],
      ['gross_buy_back', 'Buying back the sold legs alone', estimate.gross_buy_back],
    ];
    return rows.map(([key, label, figure]) => ({ key, label, figure, terms: figure.terms ?? [] }));
  });

  protected readonly legs = computed<readonly LegRow[]>(() =>
    this.estimate().quotes.map((leg) => ({
      leg,
      side: leg.contracts < 0 ? 'sold' : 'bought',
      count: Math.abs(leg.contracts),
      ageSeconds: leg.age_ms === null || leg.age_ms === undefined ? null : ageSeconds(leg.age_ms),
      missing: leg.missing && leg.missing.length > 0 ? `no ${leg.missing.join(' or ')}` : null,
    })),
  );
}
