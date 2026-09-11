import { ChangeDetectionStrategy, Component, computed, input, linkedSignal, output } from '@angular/core';
import { FormField, form } from '@angular/forms/signals';

import type {
  BrokerObservedAccount,
  BrokerProfileRevision,
} from '../../../../api/alpaca.types';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';

/**
 * Which broker account this revision actually reaches, as read-only evidence.
 *
 * Nobody types an account ID here, by owner decision: verification asks the
 * broker which account the revision's slot and mode reach, and the operator
 * pins one of the accounts it observed. The IDs render byte-for-byte — they
 * are audit identity, not a label — while the nickname beside them is the
 * label, keyed to the account so one account reads the same on every surface.
 *
 * The declared endpoint mode and the observed account mode are shown as two
 * separate facts rather than one. When they disagree, that disagreement is the
 * evidence; collapsing them into a single badge would hide it.
 */
@Component({
  selector: 'app-configuration-account-evidence',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormField, ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './configuration-account-evidence.component.html',
  styleUrl: './configuration-account-evidence.component.scss',
  host: { class: 'block' },
})
export class ConfigurationAccountEvidenceComponent {
  readonly revision = input.required<BrokerProfileRevision>();
  /** The last verification's observations, or `null` when none has been run here. */
  readonly observed = input<readonly BrokerObservedAccount[] | null>(null);
  readonly nickname = input<string | null>(null);
  readonly busy = input(false);

  readonly verifyRequested = output();
  readonly pinRequested = output<string>();
  readonly nicknameSubmitted = output<string>();

  // Seeded from a `computed` with an explicit `equal`, like every other draft on
  // this surface: a `linkedSignal` source *function* re-seeds when its
  // dependencies change even if its value has not, which discards typing.
  private readonly storedNickname = computed(() => this.nickname() ?? '', {
    equal: (a, b) => a === b,
  });
  protected readonly nicknameDraft = linkedSignal<string, string>({
    source: this.storedNickname,
    computation: (nickname) => nickname,
  });
  protected readonly nicknameForm = form(this.nicknameDraft);

  protected readonly pinnedAccountId = computed(() => this.revision().account_pin);

  protected readonly canSaveNickname = computed(
    () => !this.busy() && this.pinnedAccountId() !== null && this.nicknameDraft().trim().length > 0,
  );

  protected saveNickname(): void {
    if (!this.canSaveNickname()) return;
    this.nicknameSubmitted.emit(this.nicknameDraft().trim());
  }
}
