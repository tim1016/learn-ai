import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import type { components } from '../../../api/broker.types';
import { OptionContractComponent } from '../../../shared/option-contract/option-contract.component';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';

export type OptionDeployReview = components['schemas']['OptionDeployReview'];

/**
 * What Deploy's Money step says of a bot that trades an option structure
 * (#2915): what one entry would pick and set aside right now, as the server
 * reviewed it beside the budget.
 *
 * Everything here is the server's: each row's label, its dollars and its
 * note, in the order the rows came; the sentences; the notes. Nothing is
 * added up, signed or named here. A premium carries no sign: its note says
 * paid or received. The rows the server marks as part of what is set aside
 * are drawn under the set-aside row, in its order. A row whose figure is
 * stale is said so with the time it was read, and one that is unknown says
 * so beside the note that tells why (#3099). Each pick shows the price it is
 * assumed at, and how many entries fit comes in the server's sentence.
 *
 * A review whose preview read no quotes carries no picks, no rows and no
 * read time, and this shows none. What stops a Deploy is not said here: the
 * blockers are Confirm's, in its "Can't deploy yet" list.
 */
@Component({
  selector: 'app-deploy-option-money-review',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [OptionContractComponent, TimestampDisplayComponent],
  templateUrl: './deploy-option-money-review.component.html',
  styleUrl: './deploy-option-money-review.component.scss',
})
export class DeployOptionMoneyReviewComponent {
  readonly review = input.required<OptionDeployReview>();
  /**
   * The budget's own line already says where the preview stands (a wait for
   * a price says so in its sentence), so the preview's sentence is not said
   * a second time under it.
   */
  readonly stateSaid = input(false);
}
