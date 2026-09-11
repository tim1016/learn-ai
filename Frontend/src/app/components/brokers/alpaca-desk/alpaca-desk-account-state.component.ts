import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type {
  AlpacaDeskSelectionSummary,
  AlpacaDeskState,
} from '../../../api/alpaca.types';
import { AlpacaAccountActivationComponent } from './alpaca-account-activation.component';
import { AlpacaAccountCardComponent } from './alpaca-account-card.component';

/**
 * Configuration/connection state above the operating desk. This keeps the
 * shell's trading controls independent from the four activation states while
 * preserving the account card's shared account read.
 */
@Component({
  selector: 'app-alpaca-desk-account-state',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AlpacaAccountActivationComponent, AlpacaAccountCardComponent],
  templateUrl: './alpaca-desk-account-state.component.html',
  styleUrl: './alpaca-desk-account-state.component.scss',
})
export class AlpacaDeskAccountStateComponent {
  readonly state = input<AlpacaDeskState | null>(null);
  readonly accountAvailable = input.required<boolean>();
  readonly accountFailed = input.required<boolean>();

  readonly reviewRequested = output<AlpacaDeskSelectionSummary | null>();

  protected readonly showActivation = computed(
    () => this.accountFailed() && this.state()?.effective_choice === null,
  );
  protected readonly showConnectivityContext = computed(() => {
    const state = this.state();
    return this.accountFailed() && state !== null && state.effective_choice !== null;
  });
  protected readonly showSelectionChange = computed(() => {
    const state = this.state();
    return this.accountAvailable()
      && state !== null
      && state.staged_choice !== null
      && state.activation_state !== 'effective_selection';
  });

  protected actionTarget(state: AlpacaDeskState): AlpacaDeskSelectionSummary | null {
    return state.action.kind === 'review_configuration'
      ? state.effective_choice
      : state.staged_choice;
  }

  protected requestReview(
    state: AlpacaDeskState,
    target: AlpacaDeskSelectionSummary | null,
  ): void {
    if (!state.action.enabled) return;
    this.reviewRequested.emit(target);
  }
}
