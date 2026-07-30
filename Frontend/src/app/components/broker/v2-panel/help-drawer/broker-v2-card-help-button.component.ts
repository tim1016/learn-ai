import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { BrokerV2HelpDrawerService } from './broker-v2-help-drawer.service';
import {
  BROKER_V2_CARD_ANCHOR_MAP,
  type BrokerV2CardName,
} from './broker-v2-card-anchor-map';

/**
 * Reusable "?" button that opens the broker-v2 operator manual at the section
 * mapped to a specific card name.
 *
 * The `cardName` input is typed as `BrokerV2CardName` — a union of keys in
 * `BROKER_V2_CARD_ANCHOR_MAP`.  Templates receive a compile-time error if they
 * pass an unrecognised card name.
 *
 * S4 wiring note: S4 operator-lens card components will wire `cardName` once
 * the S4 branch is rebased onto this branch.  The anchor lookup and type are
 * intentionally frozen here so S4 has a stable contract to target.
 *
 * @example
 * <app-broker-v2-card-help-btn cardName="signal" />
 * <app-broker-v2-card-help-btn cardName="submit-gate" label="Submit gate help" />
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
  /**
   * The card whose manual section to open.  Must be a key in
   * `BROKER_V2_CARD_ANCHOR_MAP` — passing an arbitrary string is a
   * compile-time error.
   */
  readonly cardName = input.required<BrokerV2CardName>();

  /** Aria-label for the button.  Defaults to "Open manual help". */
  readonly label = input<string>('');

  private readonly anchor = computed(() => BROKER_V2_CARD_ANCHOR_MAP[this.cardName()]);
  private readonly helpDrawer = inject(BrokerV2HelpDrawerService);

  openHelp(): void {
    this.helpDrawer.open(this.anchor());
  }
}
