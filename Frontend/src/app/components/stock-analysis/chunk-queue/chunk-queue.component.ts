import { Component, input, output, computed, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { TableModule } from 'primeng/table';
import { FetchChunk } from '../models';

@Component({
  selector: 'app-chunk-queue',
  standalone: true,
  imports: [CommonModule, TableModule],
  templateUrl: './chunk-queue.component.html',
  styleUrls: ['./chunk-queue.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ChunkQueueComponent {
  chunks = input<FetchChunk[]>([]);
  chunkSelected = output<FetchChunk>();
  chunkRefresh = output<FetchChunk>();

  completedCount = computed(() => this.chunks().filter(c => c.status === 'complete').length);
  totalBars = computed(() => this.chunks().reduce((sum, c) => sum + c.barCount, 0));

  onRowClick(chunk: FetchChunk): void {
    if (chunk.status === 'complete') {
      this.chunkSelected.emit(chunk);
    }
  }

  onRefreshClick(event: Event, chunk: FetchChunk): void {
    event.stopPropagation();
    this.chunkRefresh.emit(chunk);
  }

  formatDuration(ms: number): string {
    if (ms === 0) return '—';
    if (ms < 1000) return `${ms}ms`;
    return `${(ms / 1000).toFixed(1)}s`;
  }
}
