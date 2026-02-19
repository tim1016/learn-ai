import {
  Component, ChangeDetectionStrategy, inject, computed,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { ReplayEngineService } from '../../../services/replay-engine.service';

@Component({
  selector: 'app-replay-controls',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './replay-controls.component.html',
  styleUrls: ['./replay-controls.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ReplayControlsComponent {
  readonly replayEngine = inject(ReplayEngineService);

  readonly speeds = [1, 2, 5, 10, 50];

  readonly isPlaying = computed(() => this.replayEngine.playbackState() === 'playing');

  readonly currentTimestamp = computed(() => {
    const bar = this.replayEngine.currentBar();
    if (!bar) return '';
    const d = new Date(bar.timestamp);
    return d.toLocaleString('en-US', {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  });

  readonly progressPercent = computed(() =>
    Math.round(this.replayEngine.progress() * 100)
  );

  togglePlay(): void {
    if (this.isPlaying()) {
      this.replayEngine.pause();
    } else {
      this.replayEngine.play();
    }
  }

  onSliderInput(event: Event): void {
    const value = (event.target as HTMLInputElement).valueAsNumber;
    this.replayEngine.seekToPercent(value / 100);
  }
}
