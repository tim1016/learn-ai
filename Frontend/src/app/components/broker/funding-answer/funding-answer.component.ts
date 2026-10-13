import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import type { FigureView, FundingView } from '../v2-panel/lib/broker-v2-panel.service';

type LimitName = NonNullable<FundingView['binding']>;

interface LimitRow {
  readonly key: LimitName;
  readonly label: string;
  readonly figure: FigureView;
  readonly terms: NonNullable<FigureView['terms']>;
  readonly binds: boolean;
}

/**
 * Whether a bot can fund its next entry, and by which limit it cannot (#3097).
 *
 * Purely presentational. Python authors the sentence, the action, the
 * shortfall and every figure with its terms; this card lists the limits in
 * the Clerk's own order, marks the one the answer says binds, and says a
 * stale or unknown figure is one. It compares nothing and adds nothing, so a
 * row can never disagree with the Clerk's verdict.
 */
@Component({
  selector: 'app-funding-answer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AuthoredUsdPipe, TimestampDisplayComponent],
  templateUrl: './funding-answer.component.html',
  styleUrl: './funding-answer.component.scss',
})
export class FundingAnswerComponent {
  readonly funding = input.required<FundingView>();

  protected readonly rows = computed<readonly LimitRow[]>(() => {
    const funding = this.funding();
    const limits: readonly [LimitName, string, FigureView | null | undefined][] = [
      ['bot_budget', 'Free budget', funding.free_budget],
      ['account_headroom', 'Account headroom', funding.account_headroom],
      ['broker_buying_power', 'Alpaca option buying power', funding.broker_buying_power],
    ];
    return limits.flatMap(([key, label, figure]) =>
      figure ? [{ key, label, figure, terms: figure.terms ?? [], binds: funding.binding === key }] : [],
    );
  });
}
