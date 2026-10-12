import { ChangeDetectionStrategy, Component, computed, inject, input, signal } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import { FileDownloadService } from '../../../../shared/file/file-download.service';
import { extractServerMessage } from '../../../broker/operation-error';
import { RunDetailRefusedError, RunDetailService, UNNAMED_EXPORT } from '../run-detail.service';

let nextNoteId = 0;

/**
 * Saves every trade of a complete run as the server's CSV. The page does not build, read or re-encode the file: the
 * bytes the server sent are handed to the browser as they came, under the name the server gave them. While the file
 * comes the button is busy; a failure is the server's words, shown beside it and about this run only.
 */
@Component({
  selector: 'app-options-backtest-run-export',
  imports: [ButtonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './run-export.component.html',
  styleUrl: './run-export.component.scss',
})
export class RunExportComponent {
  private readonly service = inject(RunDetailService);
  private readonly download = inject(FileDownloadService);

  readonly runId = input.required<number>();
  protected readonly noteId = `ob-export-note-${nextNoteId++}`;

  private readonly busyFor = signal<number | null>(null);
  private readonly failure = signal<{ readonly runId: number; readonly text: string } | null>(null);
  protected readonly busy = computed(() => this.busyFor() === this.runId());
  /** Why the last export of this run failed. A failure of another run is not this run's to show. */
  protected readonly problem = computed(() => {
    const failure = this.failure();
    return failure !== null && failure.runId === this.runId() ? failure.text : null;
  });

  protected async save(): Promise<void> {
    // A press that reaches here while the file is coming (a key held down, a script) asks for nothing more.
    if (this.busy()) return;
    const runId = this.runId();
    this.busyFor.set(runId);
    this.failure.set(null);
    try {
      const file = await this.service.tradesCsv(runId);
      this.download.save(file.blob, file.filename ?? UNNAMED_EXPORT);
    } catch (error) {
      const text = error instanceof RunDetailRefusedError ? error.message : extractServerMessage(error, 'The trades file could not be made. Try again.');
      this.failure.set({ runId, text });
    } finally {
      if (this.busyFor() === runId) this.busyFor.set(null);
    }
  }
}
