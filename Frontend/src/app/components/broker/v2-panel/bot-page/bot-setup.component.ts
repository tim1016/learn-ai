import { ChangeDetectionStrategy, Component, computed, input, model } from '@angular/core';

import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import type { BotPanelView } from '../lib/broker-v2-panel.types';

/** A deployed setting as the panel lists it. */
interface SettingLine {
  readonly name: string;
  readonly value: string;
}

/**
 * What the bot was told to do (#2794 R5): its strategy and deployed
 * settings, its end, its exit terms and whether its build is proven. The
 * build proof's hashes sit behind Build proof, each with a copy button --
 * operator detail one click away, not on the page.
 */
@Component({
  selector: 'app-bot-setup',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './bot-setup.component.html',
  styleUrl: './bot-setup.component.scss',
})
export class BotSetupComponent {
  readonly panel = input.required<BotPanelView>();
  /** The deployed settings by name, as the strategy view read them; `null` before it has. */
  readonly settings = input<Readonly<Record<string, unknown>> | null>(null);
  /** Whether the build proof's hashes are shown; the toolbar's Build proof opens them. */
  readonly proofOpen = model(false);

  protected readonly settingLines = computed((): readonly SettingLine[] =>
    Object.entries(this.settings() ?? {})
      .filter(([name, value]) => name !== 'symbol' && value !== null && typeof value !== 'object')
      .map(([name, value]) => ({ name, value: String(value) })),
  );

  protected copy(text: string): void {
    void globalThis.navigator?.clipboard?.writeText(text);
  }
}
