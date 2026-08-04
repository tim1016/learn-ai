import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { RouterLink } from '@angular/router';
import type { BotPanelView } from '../lib/broker-v2-panel.types';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';

@Component({
  selector: 'app-panel-header',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, RouterLink, TimestampDisplayComponent],
  templateUrl: './panel-header.component.html',
  styleUrl: './panel-header.component.scss',
})
export class PanelHeaderComponent {
  readonly panel = input.required<BotPanelView>();

  protected formatAge(ageMs: number | null): string {
    if (ageMs === null) return 'No bar received';
    if (ageMs < 60_000) return `${Math.floor(ageMs / 1_000)}s old`;
    return `${Math.floor(ageMs / 60_000)}m old`;
  }
}
