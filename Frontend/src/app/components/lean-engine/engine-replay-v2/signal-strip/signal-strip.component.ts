import {
  Component, ChangeDetectionStrategy, inject, computed,
} from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { ReplayEngineV2Service, SignalCard } from '../services/replay-engine-v2.service';

interface SparklinePath {
  card: SignalCard;
  d: string;
  trendClass: string;
}

const SPARK_W = 68;
const SPARK_H = 24;

@Component({
  selector: 'app-signal-strip',
  standalone: true,
  imports: [DecimalPipe],
  templateUrl: './signal-strip.component.html',
  styleUrls: ['./signal-strip.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SignalStripComponent {
  private readonly svc = inject(ReplayEngineV2Service);

  readonly cards = computed<SparklinePath[]>(() => {
    return this.svc.signalCards().map(card => ({
      card,
      d: buildPath(card.sparkline),
      trendClass: card.delta === null
        ? 'flat'
        : card.delta > 0 ? 'up' : card.delta < 0 ? 'down' : 'flat',
    }));
  });

  readonly active = computed(() => this.svc.activePosition() !== null);
  readonly empty = computed(() => this.svc.signalCards().length === 0);

  readonly sparkWidth = SPARK_W;
  readonly sparkHeight = SPARK_H;
  readonly viewBox = `0 0 ${SPARK_W} ${SPARK_H}`;
}

function buildPath(values: number[]): string {
  if (values.length < 2) return '';
  let lo = Infinity, hi = -Infinity;
  for (const v of values) {
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  const range = hi - lo || 1;
  const dx = SPARK_W / (values.length - 1);
  return values
    .map((v, i) => {
      const x = (i * dx).toFixed(2);
      const y = (SPARK_H - ((v - lo) / range) * SPARK_H).toFixed(2);
      return `${i === 0 ? 'M' : 'L'}${x},${y}`;
    })
    .join(' ');
}
