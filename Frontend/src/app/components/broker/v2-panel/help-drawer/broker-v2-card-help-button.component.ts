import { ChangeDetectionStrategy, Component, inject, input } from '@angular/core';
import { BrokerV2HelpDrawerService } from './broker-v2-help-drawer.service';

/**
 * Reusable "?" button that opens the broker-v2 help drawer at a specific
 * manual section. Import this component into any card that wants to surface
 * contextual help.
 *
 * @example
 * <app-broker-v2-card-help-btn anchor="station-3-submit-gate" />
 */
@Component({
  selector: 'app-broker-v2-card-help-btn',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <button
      type="button"
      class="help-btn"
      [attr.aria-label]="label() || 'Open manual help'"
      (click)="openHelp()"
    >
      <i class="pi pi-question-circle" aria-hidden="true"></i>
    </button>
  `,
  styles: [
    `
      .help-btn {
        background: transparent;
        border: 1px solid var(--border);
        color: var(--text-muted);
        width: 24px;
        height: 24px;
        border-radius: 4px;
        cursor: pointer;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        font-size: 0.85rem;
        transition: background 0.1s, color 0.1s;

        &:hover {
          background: var(--bg-hover);
          color: var(--text-primary);
        }
      }
    `,
  ],
})
export class BrokerV2CardHelpButtonComponent {
  /** The anchor slug (without `#`) to scroll to in the manual. */
  readonly anchor = input.required<string>();

  /** Aria-label for the button. Defaults to "Open manual help". */
  readonly label = input<string>('');

  private readonly helpDrawer = inject(BrokerV2HelpDrawerService);

  openHelp(): void {
    this.helpDrawer.open(this.anchor());
  }
}
