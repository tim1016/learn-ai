import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { RouterLink } from '@angular/router';

export interface LastSessionNotice {
  tone: 'ok' | 'warn' | 'bad';
  title: string;
  detail: string;
  fix: string;
}

export interface LastSessionLogTarget {
  runId: string;
  live: boolean;
}

/**
 * "Last Session" — answers *"why did the most recent session end?"* with two
 * layouts depending on severity:
 *
 *   - **Clean** (notice.tone === 'ok'): a thin one-line stub so the operator
 *     gets explicit confirmation the page isn't hiding a problem (User Story
 *     #15), rather than the previous layout's silent-when-clean behavior.
 *   - **Dirty** (notice.tone === 'warn' | 'bad'): a full card that leads with
 *     the human-language title (User Story #16, #51), shows the explanation
 *     detail + plain-language fix line, and exposes a Re-deploy button when
 *     the bound run's ledger supports it plus a "View run log" affordance
 *     when a log exists.
 *
 * The notice itself is computed by the parent (the engine's exit_reason +
 * halt_trigger taxonomy needs cross-field reasoning that lives on the page
 * model). This card is the presentation layer — given a notice, render the
 * right shape.
 */
@Component({
  selector: 'app-last-session-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink],
  templateUrl: './last-session-card.component.html',
  styleUrl: './last-session-card.component.scss',
})
export class LastSessionCardComponent {
  readonly notice = input.required<LastSessionNotice | null>();
  readonly canRedeploy = input.required<boolean>();
  readonly redeployQueryParams = input.required<Record<string, string>>();
  readonly runLogTarget = input.required<LastSessionLogTarget | null>();

  readonly isClean = computed<boolean>(() => this.notice()?.tone === 'ok');
  readonly hasContent = computed<boolean>(() => this.notice() !== null);

  /** Fired when the operator clicks "View run log" on the dirty layout. */
  readonly viewRunLogRequested = output<LastSessionLogTarget>();

  onViewRunLogClick(): void {
    const target = this.runLogTarget();
    if (target) this.viewRunLogRequested.emit(target);
  }
}
