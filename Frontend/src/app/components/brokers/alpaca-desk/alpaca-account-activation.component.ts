import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  linkedSignal,
  output,
} from '@angular/core';

import type {
  AlpacaDeskSelectionSummary,
  AlpacaDeskState,
} from '../../../api/alpaca.types';

/**
 * Pure no-effective-account presentation. The container owns reads, writes,
 * routing, and stale-write handling; this component renders server-owned copy
 * and reports the operator's selection and action intent.
 */
@Component({
  selector: 'app-alpaca-account-activation',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './alpaca-account-activation.component.html',
  styleUrl: './alpaca-account-activation.component.scss',
})
export class AlpacaAccountActivationComponent {
  readonly view = input.required<AlpacaDeskState>();
  readonly busy = input(false);

  readonly reviewRequested = output<AlpacaDeskSelectionSummary | null>();

  protected readonly selectedSelectionId = linkedSignal(
    () => this.view().staged_choice?.selection_id ?? null,
  );
  protected readonly selectedChoice = computed(() => {
    const selectionId = this.selectedSelectionId();
    return this.view().choices.find((choice) => choice.selection_id === selectionId) ?? null;
  });
  private readonly actionDestination = computed(
    () => this.selectedChoice() ?? this.view().staged_choice,
  );
  protected readonly actionLabel = computed(
    () => this.selectedChoice()?.action_label ?? this.view().action.label,
  );
  protected readonly actionConsequence = computed(
    () => this.selectedChoice()?.action_consequence ?? this.view().consequence,
  );
  protected readonly canRequestAction = computed(() => {
    const action = this.view().action;
    return !this.busy()
      && action.enabled
      && (
        this.actionDestination() !== null
        || (this.view().choices.length === 0 && action.kind === 'review_configuration')
      );
  });

  protected selectChoice(selectionId: string): void {
    this.selectedSelectionId.set(selectionId);
  }

  protected requestAction(): void {
    if (!this.canRequestAction()) return;
    this.reviewRequested.emit(this.actionDestination());
  }
}
