import { NgTemplateOutlet } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import type { FigureView } from '../v2-panel/lib/broker-v2-panel.service';

/**
 * One figure the backend authored, with its amount, its state, where it came from and how old it is, and the
 * terms it is made of when the row is opened (#3101).
 *
 * Purely presentational: it adds, compares and rounds nothing, and an unknown figure has no amount here, never
 * "$0.00". A stale one says so beside the amount it last had. A host that wants the source and the time
 * visible without opening the row says so (`showProvenance`); otherwise they are inside it.
 */
@Component({
  selector: 'app-figure-row',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AuthoredUsdPipe, NgTemplateOutlet, TimestampDisplayComponent],
  templateUrl: './figure-row.component.html',
  styleUrl: './figure-row.component.scss',
})
export class FigureRowComponent {
  readonly label = input.required<string>();
  readonly figure = input.required<FigureView>();
  readonly showProvenance = input(false);

  protected readonly terms = computed(() => this.figure().terms ?? []);
}
