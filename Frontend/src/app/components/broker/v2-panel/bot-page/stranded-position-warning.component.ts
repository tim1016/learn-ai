import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  inject,
  input,
  output,
  signal,
  viewChild,
} from '@angular/core';

import type { BotPanelView } from '../lib/broker-v2-panel.types';
import { ExposureNoticesComponent } from '../startup-join/exposure-notices.component';
import type { FlattenRequest, FlattenStepState, FlattenStepView } from './flatten-sequence';

const STEP_STATE_TEXT: Readonly<Record<FlattenStepState, string>> = {
  waiting: 'Waiting',
  running: 'In progress',
  done: 'Done',
  failed: 'Stopped',
};

/**
 * The fix beside the problem (PRD #2560 D2, stories 44–45; hurdles H30–H32):
 * a stopped bot that still holds shares says no bot is managing them, why it
 * stopped, and offers Flatten with an inline confirmation that names exactly
 * what will be sold. Confirming runs the whole flatten sequence; its steps
 * report here as they go.
 */
@Component({
  selector: 'app-stranded-position-warning',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ExposureNoticesComponent],
  templateUrl: './stranded-position-warning.component.html',
  styleUrl: './stranded-position-warning.component.scss',
})
export class StrandedPositionWarningComponent {
  readonly panel = input.required<BotPanelView>();
  /** The flatten sequence's steps, once one has started on this page. */
  readonly steps = input<readonly FlattenStepView[] | null>(null);
  readonly pending = input(false);

  readonly flatten = output<FlattenRequest>();

  private readonly injector = inject(Injector);
  private readonly flattenButton = viewChild<ElementRef<HTMLButtonElement>>('flattenButton');
  private readonly confirmButton = viewChild<ElementRef<HTMLButtonElement>>('confirmButton');

  protected readonly stateText = STEP_STATE_TEXT;
  protected readonly confirming = signal(false);

  protected readonly positions = computed<readonly FlattenRequest[]>(() =>
    Object.entries(this.panel().exposure)
      .filter(([, quantity]) => quantity !== 0)
      .map(([symbol, quantity]) => ({ symbol, quantity })),
  );

  protected readonly heading = computed(() =>
    `No bot is managing ${this.positions().map((p) => `${p.quantity} ${p.symbol}`).join(' and ')}`,
  );

  /** One bot holds one symbol; a flatten sells exactly that one position. */
  protected readonly position = computed(() => {
    const positions = this.positions();
    return positions.length === 1 ? positions[0] : null;
  });

  protected readonly dutyOutcome = computed(() => this.panel().health.duty_outcome ?? null);

  protected askToFlatten(): void {
    this.confirming.set(true);
    afterNextRender(() => this.confirmButton()?.nativeElement.focus(), { injector: this.injector });
  }

  protected cancel(): void {
    this.confirming.set(false);
    afterNextRender(() => this.flattenButton()?.nativeElement.focus(), { injector: this.injector });
  }

  protected confirm(position: FlattenRequest): void {
    this.confirming.set(false);
    this.flatten.emit(position);
  }
}
