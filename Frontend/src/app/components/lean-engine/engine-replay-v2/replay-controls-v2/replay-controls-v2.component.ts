import {
  Component, ChangeDetectionStrategy, inject, computed,
  ElementRef, viewChild,
} from '@angular/core';
import { DatePipe } from '@angular/common';
import { Tooltip } from 'primeng/tooltip';
import {
  ReplayEngineV2Service, Direction, WindowSize,
} from '../services/replay-engine-v2.service';

interface TradePip {
  xPct: number;
  kind: 'win' | 'loss';
}

interface WindowBracket {
  leftPct: number;
  widthPct: number;
}

@Component({
  selector: 'app-replay-controls-v2',
  standalone: true,
  imports: [DatePipe, Tooltip],
  templateUrl: './replay-controls-v2.component.html',
  styleUrls: ['./replay-controls-v2.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ReplayControlsV2Component {
  readonly svc = inject(ReplayEngineV2Service);
  readonly minimap = viewChild.required<ElementRef<HTMLDivElement>>('minimap');

  readonly speeds = [1, 2, 4, 10] as const;
  readonly windowOptions: readonly (readonly [string, WindowSize])[] = [
    ['100', 100],
    ['200', 200],
    ['500', 500],
    ['all', 'all'],
  ] as const;

  readonly isPlaying = computed(() => this.svc.playbackState() === 'playing');
  readonly isForward = computed(() => this.svc.direction() === 'forward');

  readonly tradePips = computed<TradePip[]>(() => {
    const trades = this.svc.trades();
    const total = this.svc.totalBars();
    const bars = this.svc.bars();
    if (total <= 1 || trades.length === 0) return [];
    const firstMs = new Date(bars[0].timestamp).getTime();
    const lastMs = new Date(bars[total - 1].timestamp).getTime();
    const span = lastMs - firstMs || 1;
    return trades.map(t => ({
      xPct: ((t.entryMs - firstMs) / span) * 100,
      kind: t.pnl >= 0 ? ('win' as const) : ('loss' as const),
    }));
  });

  readonly windowBracket = computed<WindowBracket>(() => {
    const total = this.svc.totalBars();
    const win = this.svc.renderWindow();
    if (total <= 1) return { leftPct: 0, widthPct: 0 };
    const leftPct = (win.startIndex / (total - 1)) * 100;
    const rightPct = (win.endIndex / (total - 1)) * 100;
    return { leftPct, widthPct: Math.max(1, rightPct - leftPct) };
  });

  readonly cursorPct = computed(() => Math.round(this.svc.progress() * 100));

  readonly currentTime = computed(() => {
    const b = this.svc.currentBar();
    return b ? b.timestamp : null;
  });

  readonly indexLabel = computed(() => {
    const total = this.svc.totalBars();
    if (total === 0) return '—';
    return `${this.svc.currentIndex() + 1} / ${total}`;
  });

  togglePlay(): void {
    if (this.isPlaying()) this.svc.pause();
    else this.svc.play();
  }

  toggleDirection(): void {
    this.svc.toggleDirection();
  }

  setSpeed(mult: number): void {
    this.svc.setSpeed(mult);
  }

  setWindow(size: WindowSize): void {
    this.svc.setWindowSize(size);
  }

  stepBack(): void {
    this.svc.stepBackward();
  }

  stepFwd(): void {
    this.svc.stepForward();
  }

  onMinimapClick(evt: MouseEvent): void {
    const el = this.minimap().nativeElement;
    const rect = el.getBoundingClientRect();
    const pct = Math.max(0, Math.min(1, (evt.clientX - rect.left) / rect.width));
    this.svc.seekToPercent(pct);
  }

  onMinimapKeydown(evt: KeyboardEvent): void {
    const current = this.svc.progress();
    const next = this.nextMinimapPercent(evt.key, current);
    if (next === null) return;
    evt.preventDefault();
    this.svc.seekToPercent(Math.max(0, Math.min(1, next)));
  }

  directionLabel(d: Direction): string {
    return d === 'forward' ? '▶' : '◀';
  }

  private nextMinimapPercent(key: string, current: number): number | null {
    switch (key) {
      case 'ArrowLeft':
      case 'ArrowDown':
        return current - 0.01;
      case 'ArrowRight':
      case 'ArrowUp':
        return current + 0.01;
      case 'PageDown':
        return current - 0.1;
      case 'PageUp':
        return current + 0.1;
      case 'Home':
        return 0;
      case 'End':
        return 1;
      default:
        return null;
    }
  }
}
