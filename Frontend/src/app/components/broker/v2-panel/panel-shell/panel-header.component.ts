import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { RouterLink } from '@angular/router';
import type { BotPanelView } from '../lib/broker-v2-panel.types';
import { AssetIdentityComponent } from '../../../../shared/asset-identity';
import { ReceiptLabelPipe } from '../../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';
import { CardModule } from 'primeng/card';
import { TagModule, type TagSeverity } from 'primeng/tag';

@Component({
  selector: 'app-panel-header',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AssetIdentityComponent,
    CardModule,
    ReceiptLabelPipe,
    RouterLink,
    TagModule,
    TimestampDisplayComponent,
  ],
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

  protected missionSeverity(): TagSeverity {
    switch (this.panel().mission_verdict.state) {
      case 'working': return 'success';
      case 'off_duty': return 'warn';
      case 'blocked':
      case 'retired': return 'danger';
      default: return 'info';
    }
  }

  protected marketSeverity(): TagSeverity {
    return this.panel().market_pulse.attention_required ? 'danger' : 'success';
  }
}
