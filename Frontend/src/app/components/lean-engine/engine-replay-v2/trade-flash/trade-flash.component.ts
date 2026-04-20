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

  readonly pnlClass = computed(() => {
    const ev = this.flash();
    if (!ev) return '';
    return ev.trade.pnl >= 0 ? 'gain' : 'loss';
  });

  readonly label = computed(() => {
    const ev = this.flash();
    if (!ev) return '';
    return ev.kind === 'exit' ? 'EXIT' : 'UNWIND';
  });
}
