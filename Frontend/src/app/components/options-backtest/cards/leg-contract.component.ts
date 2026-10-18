import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { FormField, type FieldTree } from '@angular/forms/signals';

import { AssetIdentityComponent } from '../../../shared/asset-identity/asset-identity.component';
import { EXPIRY_CHOICES, EXPIRY_HINTS } from '../choices';
import type { SetupLimits } from '../options-backtest.types';
import type { LegDraft } from '../setup-draft';

/** Edits one contract in place; the leg's identity and trading rules stay attached to it. */
@Component({
  selector: 'app-options-backtest-leg-contract',
  imports: [FormField, AssetIdentityComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './leg-contract.component.html',
  host: { '[class.compact]': 'compact()' },
  styleUrl: './leg-contract.component.scss',
})
export class LegContractComponent {
  readonly compact = input(false);
  readonly legForm = input.required<FieldTree<LegDraft, number>>();
  readonly name = input.required<string>();
  readonly underlying = input.required<string>();
  readonly limits = input<SetupLimits | null>(null);

  protected readonly leg = computed(() => this.legForm()().value());
  protected readonly expiries = EXPIRY_CHOICES;
  protected readonly expiryHint = computed(() => EXPIRY_HINTS[this.leg().expiryKind]);
  protected readonly nextSide = computed(() => this.leg().side === 'buy' ? 'Sell' : 'Buy');
  protected readonly steps = computed(() => this.leg().strikeKind === 'atm' ? 0 : this.leg().strikeSteps);
  protected readonly stepperOrigin = computed(() => Number.isSafeInteger(this.steps()) ? this.steps() : 0);
  protected readonly minimumSteps = computed(() => {
    const limits = this.limits();
    return limits === null ? null : -limits.max_strike_steps;
  });
  protected readonly stepInput = computed(() => Number.isFinite(this.steps()) ? String(this.steps()) : '');
  protected readonly strikeLabel = computed(() => {
    const steps = this.steps();
    if (!Number.isSafeInteger(steps)) return 'Choose a strike';
    return steps === 0 ? 'At the money' : `${Math.abs(steps)} ${Math.abs(steps) === 1 ? 'strike' : 'strikes'} ${steps > 0 ? 'above' : 'below'} ATM`;
  });
  protected readonly nearbyStrikes = [-3, -2, -1, 0, 1, 2, 3];

  protected flipRight(): void {
    this.legForm().right().value.update(right => right === 'call' ? 'put' : 'call');
  }

  protected flipSide(): void {
    this.legForm().side().value.update(side => side === 'buy' ? 'sell' : 'buy');
  }

  protected canChoose(steps: number): boolean {
    const limits = this.limits();
    return limits === null || Math.abs(steps) <= limits.max_strike_steps;
  }

  protected chooseStrike(steps: number): void {
    if (!this.canChoose(steps)) return;
    this.legForm()().value.update(leg => ({ ...leg, strikeKind: steps === 0 ? 'atm' : 'atm_offset', strikeSteps: steps }));
  }

  protected moveStrike(direction: -1 | 1): void {
    this.chooseStrike(this.stepperOrigin() + direction);
  }

  protected typeStrike(event: Event): void {
    const input = event.target;
    if (!(input instanceof HTMLInputElement)) return;
    const steps = input.value === '' ? Number.NaN : Number(input.value);
    this.legForm()().value.update(leg => ({ ...leg, strikeKind: steps === 0 ? 'atm' : 'atm_offset', strikeSteps: steps }));
  }
}
