import { ChangeDetectionStrategy, Component, inject } from '@angular/core';

import { SignalStrategySectionComponent } from '../signal-strategy-section.component';
import { SignalStrategyWorkspaceStore } from '../signal-strategy-workspace.store';
import { ProofDetailComponent } from './proof-detail.component';

/**
 * A strategy's Proof tab (PRD #3490 R18): the proof of its program and the one
 * human flag a person set on it, with the evidence behind both. Program proof is
 * about the strategy's program at its validated corpus point; it approves no
 * symbol and no settings, which Golden does, one symbol and one exact
 * configuration at a time. A strategy the proof catalog holds no record for is
 * shown as having none, and a catalog that could not be read is unknown, not empty.
 */
@Component({
  selector: 'app-signal-strategy-proof',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ProofDetailComponent, SignalStrategySectionComponent],
  template: `
    <app-signal-strategy-section
      title="Program proof"
      scope="The proof of the strategy's program, and one person's flag on it. Neither says anything about a symbol's settings."
      [read]="store.proof()"
      (retry)="store.retry('proof')"
    >
      @if (store.proof(); as state) {
        @if (state.kind === 'ready') {
          @if (state.value; as record) {
            <app-proof-detail [summary]="record" (changed)="store.retry('proof')" />
          } @else {
            <p class="note">The proof catalog holds no record for this strategy, so nothing about its proof is known.</p>
          }
        }
      }
    </app-signal-strategy-section>
  `,
  styles: `
    :host { display: block; padding: var(--space-3) var(--space-4); }
    .note { margin: 0; color: var(--text-secondary); font-size: var(--fs-sm); }
  `,
})
export class SignalStrategyProofComponent {
  protected readonly store = inject(SignalStrategyWorkspaceStore);
}
