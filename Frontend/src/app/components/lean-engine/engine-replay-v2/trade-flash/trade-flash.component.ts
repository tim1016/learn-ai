import {
  Component, ChangeDetectionStrategy, inject, computed,
} from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { ReplayEngineV2Service } from '../services/replay-engine-v2.service';

@Component({
  selector: 'app-trade-flash',
  standalone: true,
  imports: [DecimalPipe],
  templateUrl: './trade-flash.component.html',
  styleUrls: ['./trade-flash.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class TradeFlashComponent {
  private readonly svc = inject(ReplayEngineV2Service);

  readonly flash = this.svc.flashEvent;

  readonly kindClass = computed(() => {
    const ev = this.flash();
    if (!ev) return '';
    switch (ev.kind) {
      case 'buy-entry':  return 'buy';
      case 'sell-entry': return 'sell';
      case 'exit':       return ev.trade.pnl >= 0 ? 'gain' : 'loss';
      case 'unwind':     return 'unwind';
    }
  });

  readonly label = computed(() => {
    const ev = this.flash();
    if (!ev) return '';
    switch (ev.kind) {
      case 'buy-entry':  return 'BUY';
      case 'sell-entry': return 'SELL';
      case 'exit':       return 'EXIT';
      case 'unwind':     return 'UNWIND';
    }
  });

  readonly isEntry = computed(() => {
    const ev = this.flash();
    return ev ? (ev.kind === 'buy-entry' || ev.kind === 'sell-entry') : false;
  });
}
