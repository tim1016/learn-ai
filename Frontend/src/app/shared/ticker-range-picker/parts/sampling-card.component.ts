import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  model,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Tooltip } from 'primeng/tooltip';

import type {
  Resolution,
  Session,
  TickerRange,
} from '../ticker-range-picker.types';

/** Session-toggle behaviour for the picker.
 *  ``preview``  = Both options visible; "preview" tag on Extended; both
 *                 selectable but consumers may ignore Extended.
 *  ``disabled`` = Extended rendered but disabled with a tooltip.
 *  ``hidden``   = Session group not rendered at all. */
export type SessionMode = 'preview' | 'disabled' | 'hidden';

@Component({
  selector: 'app-sampling-card',
  imports: [CommonModule, FormsModule, Tooltip],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './sampling-card.component.html',
  styleUrls: ['./sampling-card.component.scss'],
})
export class SamplingCardComponent {
  readonly value = model.required<TickerRange>();
  readonly availableResolutions = input<readonly Resolution[]>([
    'minute',
    'hour',
    'daily',
  ]);
  readonly availableMultipliers = input<readonly number[]>([]);
  readonly sessionMode = input<SessionMode>('preview');
  readonly showAutoFetch = input(true);

  readonly effectiveSession = computed<Session>(
    () => this.value().session ?? 'rth',
  );
  readonly effectiveMultiplier = computed<number>(
    () => this.value().multiplier ?? 1,
  );

  setResolution(r: Resolution): void {
    this.value.set({ ...this.value(), resolution: r });
  }

  setMultiplier(m: number): void {
    this.value.set({ ...this.value(), multiplier: m });
  }

  setSession(s: Session): void {
    if (s === 'extended' && this.sessionMode() === 'disabled') return;
    this.value.set({ ...this.value(), session: s });
  }

  setAutoFetch(on: boolean): void {
    this.value.set({ ...this.value(), autoFetch: on });
  }
}
