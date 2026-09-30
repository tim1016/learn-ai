import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  input,
  model,
  output,
  viewChild,
} from '@angular/core';
import { FormField, form, readonly as readOnly } from '@angular/forms/signals';

import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import type { DeployReadinessCheck, RunAdmissionDecision } from '../v2-panel/lib/broker-v2-panel.service';
import { DeployReadinessSectionComponent } from './deploy-readiness-section.component';
import { DeployStartAdmissionComponent } from './deploy-start-admission.component';
import { DEPLOY_WORLDS, type DeployWorld } from './deploy-world';

/** A refused deployment, shaped for display by the workflow that caught it. */
export interface DeployError {
  outcome: 'conflict' | 'blocked' | 'unknown';
  title: string;
  message: string;
  explanation: string | null;
  nextAction: string | null;
  receiptId: string | null;
  recordedAtMs: number | null;
}

/** One failing check, with its fix when the backend authored one. */
export interface DeployBlocker {
  readonly id: string;
  readonly label: string;
  readonly headline: string;
  readonly fix: string | null;
}

/**
 * Deploy step 4, Confirm (PRD #2560 D8).
 *
 * The review of what is about to happen — the bot's name as the backend will
 * author it, what it trades, the account and its world, the exits, the
 * budget and the account loss limit — then "Can't deploy yet" only when a
 * check fails, Live's typed consent, and a button naming the world and the
 * amount. Every check and the last Start decision open in a popover, so the
 * step never grows past its column.
 */
@Component({
  selector: 'app-deploy-confirm-step',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AuthoredUsdPipe,
    FormField,
    DeployReadinessSectionComponent,
    DeployStartAdmissionComponent,
    TimestampDisplayComponent,
  ],
  templateUrl: './deploy-confirm-step.component.html',
  styleUrl: './deploy-confirm-step.component.scss',
})
export class DeployConfirmStepComponent {
  /** The world chosen in How, or `null` before the owner chooses. */
  readonly world = input<DeployWorld | null>(null);
  /** The backend's sentence about the name it will give the bot. */
  readonly botNameNote = input<string | null>(null);
  readonly strategyLabel = input<string | null>(null);
  readonly trades = input.required<string>();
  /** The backend's warning that other bots in this account already trade the symbol; never a block. */
  readonly sameSymbolNote = input<string | null>(null);
  /** The external account number; `null` for Dry Run, whose cash is its own. */
  readonly accountNumber = input<string | null>(null);
  readonly exits = input.required<string>();
  /** The previewed budget, as the backend authored it. */
  readonly budgetUsd = input<string | null>(null);
  readonly lossLimit = input<string | null>(null);
  readonly blockers = input<readonly DeployBlocker[]>([]);
  /** Every broker check, for the checks popover; `null` in Dry Run. */
  readonly checks = input<readonly DeployReadinessCheck[] | null>(null);
  readonly checksObservedAtMs = input<number | null>(null);
  readonly admissionDecision = input<RunAdmissionDecision | null>(null);
  readonly submitError = input<DeployError | null>(null);
  /** The last Deploy's outcome is not known: offer to read what it recorded. */
  readonly canCheckStatus = input(false);
  readonly checkingStatus = input(false);
  /** Live's phrase to type, exactly as the backend authored it. */
  readonly confirmationText = input<string | null>(null);
  readonly consent = model('');
  readonly canSubmit = input(false);
  readonly submitting = input(false);
  readonly guidance = input('');

  readonly deploy = output();
  readonly checkStatus = output();

  protected readonly consentField = form(this.consent, (consent) => {
    readOnly(consent, () => this.submitting());
  });

  protected readonly worldWording = computed(() => {
    const world = this.world();
    return world === null ? 'Choose where it trades in How' : DEPLOY_WORLDS[world].wording;
  });
  protected readonly buttonWorld = computed(() => {
    const world = this.world();
    return world === null ? '' : DEPLOY_WORLDS[world].button;
  });
  protected readonly dryRun = computed(() => this.world() === 'dry_run');
  protected readonly blockedDecision = computed(() => {
    const decision = this.admissionDecision();
    return decision?.allowed === false ? decision : null;
  });
  protected readonly blockerCount = computed(() => {
    const count = this.blockers().length;
    return count === 1 ? '1 thing' : `${count} things`;
  });

  private readonly refusal = viewChild<ElementRef<HTMLElement>>('refusal');

  /** Moves the keyboard to the refusal after a Deploy the backend declined. */
  focusRefusal(): void {
    this.refusal()?.nativeElement.focus();
  }
}
