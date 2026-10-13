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
  /** Set on the limit with the least room only: "binds" when Python says the entry falls short of it, and
   * "least room" when it does not (the entry passes, or nothing is priced to fall short of). */
  readonly mark: 'binds' | 'least room' | null;
}

/**
 * Whether a bot can fund its next entry, and by which limit it cannot (#3097).
 *
 * Purely presentational. Python authors the sentence, the action, the
 * shortfall and every figure with its terms; this card lists the limits in
 * the Clerk's own order, marks the limit with the least room (as binding only
 * where the answer carries a shortfall against it), and says a stale or
 * unknown figure is one. It compares nothing and adds nothing, so a row can
 * never disagree with the Clerk's verdict. It does not repeat the sentence:
 * the bot page's own detail already leads with it, so a host that shows no
 * such detail renders `funding.sentence` itself (`showSentence`). How many entries fit is Python's own
 * sentence, with the caveat that every bot competes for the same account headroom (#3099).
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
  /** Says Python's sentence above the limits. A host that already shows the sentence (the bot page's own
   * detail, a refused amount's outcome line) leaves it off, so it is on screen once. */
  readonly showSentence = input(false);
  /** What the shortfall is short of, for a host whose answer is not about one entry. */
  readonly shortfallLabel = input('Short of this entry by');
  /** Says how many entries fit. A host whose own review already carries that sentence leaves it off, so it is
   * on screen once. */
  readonly showFits = input(true);

  protected readonly rows = computed<readonly LimitRow[]>(() => {
    const funding = this.funding();
    const limits: readonly [LimitName, string, FigureView | null | undefined][] = [
      ['bot_budget', 'Free budget', funding.free_budget],
      ['account_headroom', 'Account headroom', funding.account_headroom],
      ['broker_buying_power', 'Alpaca option buying power', funding.broker_buying_power],
    ];
    const mark = (key: LimitName): LimitRow['mark'] => {
      if (funding.binding !== key) return null;
      return funding.shortfall_usd ? 'binds' : 'least room';
    };
    return limits.flatMap(([key, label, figure]) =>
      figure ? [{ key, label, figure, terms: figure.terms ?? [], mark: mark(key) }] : [],
    );
  });
}
