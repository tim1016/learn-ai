import { ChangeDetectionStrategy, Component, computed, effect, inject, input, linkedSignal, resource, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';

import { DateFieldComponent } from '../../../shared/date-picker';
import { etMidnightMs } from '../../../shared/date/et-midnight';
import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { SetupRequest } from '../options-backtest.types';
import { PositionPayoffComponent } from './position-payoff.component';
import { PositionPreviewService, type PreviewRequest } from './position-preview.service';

/** A read-only, latest-answer-only preview of the exact setup being edited by either host. */
@Component({
  selector: 'app-options-position-preview',
  imports: [DateFieldComponent, AuthoredUsdPipe, ReceiptLabelPipe, TimestampDisplayComponent, PositionPayoffComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './position-preview.component.html',
  styleUrl: './position-preview.component.scss',
})
export class PositionPreviewComponent {
  readonly setup = input<SetupRequest | null>(null);
  readonly startingDate = input('');
  readonly debounceMs = input(250);
  readonly forDeployment = input(false);
  private readonly service = inject(PositionPreviewService);
  protected readonly source = signal<'historical' | 'current'>('historical');
  protected readonly mode = signal<'position' | 'replay'>('position');
  protected readonly date = linkedSignal(this.startingDate);
  protected readonly replayAt = signal<number | null>(null);
  private readonly generation = signal(0);
  private readonly context = computed(() => JSON.stringify({ setup: this.setup(), date: this.date(), source: this.source() }));
  protected readonly bounds = linkedSignal<string, { open: number; close: number } | null>({ source: this.context, computation: () => null });

  private readonly query = computed<PreviewRequest | undefined>(() => {
    const setup = this.setup();
    const date = this.date();
    if (setup === null || setup.legs.length === 0 || (this.source() === 'historical' && date === '')) return undefined;
    return { setup, source: this.source(), mode: this.source() === 'current' ? 'position' : this.mode(), trading_date_ms: this.source() === 'historical' ? etMidnightMs(date) : null, replay_at_ms: this.mode() === 'replay' ? this.replayAt() : null };
  });
  protected readonly read = resource({
    params: () => { const query = this.query(); return query === undefined ? undefined : { query, generation: this.generation() }; },
    loader: async ({ params, abortSignal }) => {
      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(resolve, this.debounceMs());
        abortSignal.addEventListener('abort', () => { clearTimeout(timer); reject(new Error('Preview superseded')); }, { once: true });
      });
      return this.service.preview(params.query);
    },
  });
  protected readonly answer = computed(() => this.read.hasValue() ? this.read.value() : null);
  protected readonly problem = computed(() => {
    const error: unknown = this.read.error();
    if (error === undefined) return null;
    if (error instanceof HttpErrorResponse && typeof error.error?.detail === 'string') return error.error.detail;
    if (error instanceof HttpErrorResponse && error.status === 422) return this.forDeployment()
      ? 'This approved setup could not be previewed for the selected session. Try another session.'
      : 'The position is not valid yet. Correct the leg choices and trading rules to preview it.';
    return 'The position preview could not be read. Try again.';
  });

  constructor() {
    effect(() => { this.setup(); this.date(); this.source(); this.replayAt.set(null); });
    effect(() => {
      const value = this.answer();
      if (value?.session_open_ms != null && value.session_close_ms != null) this.bounds.set({ open: value.session_open_ms, close: value.session_close_ms });
    });
  }
  protected chooseSource(source: 'historical' | 'current'): void { this.source.set(source); }
  protected chooseMode(mode: 'position' | 'replay'): void { this.mode.set(mode); }
  protected retry(): void { this.generation.update(value => value + 1); }
  protected replay(event: Event): void {
    if (event.target instanceof HTMLInputElement) this.replayAt.set(event.target.valueAsNumber);
  }
}
