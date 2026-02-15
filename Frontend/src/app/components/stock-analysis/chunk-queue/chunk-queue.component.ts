import { Component, input, output, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FetchChunk } from '../models';

@Component({
  selector: 'app-chunk-queue',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './chunk-queue.component.html',
  styleUrls: ['./chunk-queue.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ChunkQueueComponent {
  chunks = input<FetchChunk[]>([]);
  chunkSelected = output<FetchChunk>();

  onChunkClick(chunk: FetchChunk): void {
    if (chunk.status === 'complete') {
      this.chunkSelected.emit(chunk);
    }
  }

  formatDuration(ms: number): string {
    if (ms < 1000) return `${ms}ms`;
    return `${(ms / 1000).toFixed(1)}s`;
  }
}
