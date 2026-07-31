import { CurrencyPipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from '@angular/core';
import type { BotPanelView } from '../lib/broker-v2-panel.types';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';

@Component({
  selector: 'app-trader-metrics',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe, TimestampDisplayComponent],
  templateUrl: './trader-metrics.component.html',
  styleUrl: './trader-metrics.component.scss',
})
export class TraderMetricsComponent {
  readonly panel = input.required<BotPanelView>();
  protected readonly exposure = computed(() => Object.entries(this.panel().exposure));
}
