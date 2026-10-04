import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
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

/** The fields the sampling card sets. */
export type SamplingChange = Partial<Pick<TickerRange, 'resolution' | 'multiplier' | 'session' | 'autoFetch'>>;

@Component({
  selector: 'app-sampling-card',
  imports: [CommonModule, FormsModule, Tooltip],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './sampling-card.component.html',
  styleUrls: ['./sampling-card.component.scss'],
})
export class SamplingCardComponent {
  readonly value = input.required<TickerRange>();
  /**
   * Only what a control changed, for the host to merge into the value it
   * holds now. This card's own copy can be one render behind a sibling card:
   * a date typed and a control tapped in the same instant would otherwise put
   * the old date back.
   */
  readonly changed = output<SamplingChange>();
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
    this.changed.emit({ resolution: r });
  }

  setMultiplier(m: number): void {
    this.changed.emit({ multiplier: m });
  }

  setSession(s: Session): void {
    if (s === 'extended' && this.sessionMode() === 'disabled') return;
    this.changed.emit({ session: s });
  }

  setAutoFetch(on: boolean): void {
    this.changed.emit({ autoFetch: on });
  }
}
