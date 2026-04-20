import { Component, ChangeDetectionStrategy, inject, computed } from '@angular/core';
import { DecimalPipe, DatePipe } from '@angular/common';
import { ReplayEngineV2Service } from '../services/replay-engine-v2.service';

@Component({
  selector: 'app-position-hud',
  standalone: true,
  imports: [DecimalPipe, DatePipe],
  templateUrl: './position-hud.component.html',
  styleUrls: ['./position-hud.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PositionHudComponent {
  private readonly svc = inject(ReplayEngineV2Service);

  readonly pos = this.svc.position;
  readonly bar = this.svc.currentBar;

  readonly sideLabel = computed(() => {
    const s = this.pos().side;
    return s === 'long' ? 'LONG' : s === 'short' ? 'SHORT' : 'FLAT';
  });

  readonly pnlClass = computed(() => {
    const p = this.pos().floatingPnl;
    if (p === null) return 'neutral';
    return p > 0 ? 'positive' : p < 0 ? 'negative' : 'neutral';
  });
}
