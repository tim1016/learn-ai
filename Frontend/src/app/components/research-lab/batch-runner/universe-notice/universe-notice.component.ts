import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { MemberExclusion, StudyUniverse } from '../../../../services/research.service';
import { AssetIdentityComponent } from '../../../../shared/asset-identity/asset-identity.component';

const STAGE_LABELS: Record<MemberExclusion['stage'], string> = {
  stock_bars: 'stock bars',
  option_inputs: 'option inputs',
  iv_inputs: 'IV history',
  iv_diagnostics: 'IV diagnostics',
  computation: 'computation',
};

/**
 * Every requested member of a universe that is not fully evaluated (#3524): how many are in the statistics, the
 * stage and reason that left each of the rest out, and what degraded the ones that stayed in.
 *
 * `partial` qualifies a result (every statistic divides by the evaluated count); `unevaluated` explains a run that
 * has no result because no member could be evaluated.
 */
@Component({
  selector: 'app-cross-sectional-universe-notice',
  imports: [AssetIdentityComponent],
  templateUrl: './universe-notice.component.html',
  styleUrl: './universe-notice.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class CrossSectionalUniverseNoticeComponent {
  readonly universe = input.required<StudyUniverse>();
  readonly kind = input.required<'partial' | 'unevaluated'>();

  protected readonly testId = computed(() => (this.kind() === 'partial' ? 'partial-universe' : 'failed-universe'));
  protected readonly lead = computed(() => (this.kind() === 'partial' ? 'Partial universe:' : 'Members:'));

  protected readonly text = computed(() => {
    const uni = this.universe();
    const counts = `${uni.evaluatedCount} of ${uni.requestedCount} requested members are in the statistics`;
    return this.kind() === 'partial'
      ? `${counts}, so every statistic divides by ${uni.denominator}. ${uni.inclusionPolicy.description}`
      : `${counts}. ${uni.inclusionPolicy.description}`;
  });

  /** What left a member short of what was asked, in words: FRED, the one live input, did not answer. */
  protected degradedReason(d: { rateDaysFetchFailed: number }): string {
    return `${d.rateDaysFetchFailed} day(s) were priced at the default risk-free rate because FRED did not answer`;
  }

  /** The step of the study a member was left out at, in the words the page uses for its inputs. */
  protected stageLabel(stage: MemberExclusion['stage']): string {
    return STAGE_LABELS[stage];
  }
}
