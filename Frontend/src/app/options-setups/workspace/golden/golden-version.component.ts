import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { GoldenOptionSetup } from '../../../components/options-backtest/golden-option-setups.service';
import { OptionsQualificationEvidenceComponent } from '../../../components/options-backtest/options-qualification-evidence.component';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { optionsEvidenceRoute, optionsGoldenQuery, optionsSetupRoute } from '../../options-setups.address';
import { PATH_EXPLANATION, PATH_TITLE, verdictWords, type ListedVersion } from './golden-versions';

/**
 * One approved version, with what makes it what it is: the path that made it, the run or study it came
 * from, whether its proof still holds, what limits it and what the owner excepted, and every revoke and
 * re-proof since. The path is the record's own (a complete search link, a plain backtest, or an incomplete
 * link that claims nothing), so a single-run approval is never worded as a qualified search, and a version
 * a search made with changed content is said to be linked through its study and not to match what is saved.
 *
 * It only reads. Revoking and re-proving act on the version's source run, in the review above.
 */
@Component({
  selector: 'app-options-golden-version',
  imports: [RouterLink, ReceiptLabelPipe, TimestampDisplayComponent, OptionsQualificationEvidenceComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-version.component.html',
  styleUrl: './golden-version.component.scss',
})
export class OptionsGoldenVersionComponent {
  readonly listed = input.required<ListedVersion>();
  /** The version as read by its own id: its limits, exceptions and events. `null`: it could not be read. `undefined`: it is being read. */
  readonly detail = input<GoldenOptionSetup | null | undefined>(undefined);
  /** The saved name the version is listed under, for the address of the review that revokes or re-proves it. */
  readonly setupName = input.required<string>();

  protected readonly approval = computed(() => this.listed().approval);
  protected readonly pathTitle = computed(() => PATH_TITLE[this.approval().origin]);
  protected readonly pathExplanation = computed(() => PATH_EXPLANATION[this.approval().origin]);
  protected readonly status = computed(() => this.detail()?.status ?? this.approval().status);
  protected readonly verdict = computed(() => {
    const detail = this.detail();
    return detail === null || detail === undefined || this.approval().origin !== 'search' ? null : verdictWords(detail.evidence);
  });
  /** What happened to the version since it was approved, oldest first. */
  protected readonly events = computed(() => [...(this.detail()?.events ?? [])].sort((a, b) => a.created_at_ms - b.created_at_ms || a.id - b.id));
  protected readonly reviewRoute = computed(() => optionsSetupRoute(this.setupName(), 'golden'));
  protected readonly reviewQuery = computed(() => optionsGoldenQuery(this.approval().source_run_id));
  protected readonly evidenceRoute = optionsEvidenceRoute;
}
