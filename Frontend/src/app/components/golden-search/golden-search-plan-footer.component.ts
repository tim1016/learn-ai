import { DecimalPipe } from '@angular/common';
import { afterNextRender, ChangeDetectionStrategy, Component, computed, DestroyRef, ElementRef, inject, input, output, signal, viewChild } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import { TimestampDisplayComponent } from '../../shared/timestamp';
import { durationRangeText } from './golden-search-display';
import type { PlanProblem } from './golden-search-plan-problems';
import type { GoldenSearchPreflight } from './golden-search.types';

/**
 * The Plan step's footer (#2696), kept in view while the form scrolls: the
 * server's run bound against the cap and its time estimate, whether the plan
 * is ready, every problem in plain words linking to its field, Lock, and on
 * request the workload by stage and the fold plan. It is fixed to the window
 * rather than sticky, because the shell's `main` scrolls horizontally and so
 * would hold a sticky footer in place; a spacer of the footer's measured
 * height keeps the end of the form clear of it.
 */
@Component({
  selector: 'app-golden-search-plan-footer',
  imports: [ButtonModule, DecimalPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-plan-footer.component.html',
  styleUrl: './golden-search-plan-footer.component.scss',
})
export class GoldenSearchPlanFooterComponent {
  readonly preflight = input<GoldenSearchPreflight | null>(null);
  readonly checking = input(false);
  /** Why the plan could not be checked (a failed request). */
  readonly error = input<string | null>(null);
  /** Why the plan is not being checked yet (unreadable inputs, dates still being laid out). */
  readonly blocked = input<string | null>(null);
  /** Values the form cannot read yet. */
  readonly unreadable = input<readonly PlanProblem[]>([]);
  /** The server's refusals of the plan as checked. */
  readonly refusals = input<readonly PlanProblem[]>([]);
  /** Why the last Lock did not lock. */
  readonly lockMessage = input<string | null>(null);
  readonly canLock = input(false);
  readonly revising = input(false);
  readonly lock = output();
  /** The id of the input a problem belongs to. */
  readonly jump = output<string>();

  protected readonly detailsOpen = signal(false);
  /** The fixed panel's height, reserved at the end of the form. */
  protected readonly reserved = signal(0);
  private readonly panel = viewChild.required<ElementRef<HTMLElement>>('panel');

  constructor() {
    const destroyRef = inject(DestroyRef);
    afterNextRender(() => {
      if (typeof ResizeObserver === 'undefined') return;
      const observer = new ResizeObserver(() => this.reserved.set(Math.ceil(this.panel().nativeElement.getBoundingClientRect().height)));
      observer.observe(this.panel().nativeElement);
      destroyRef.onDestroy(() => observer.disconnect());
    });
  }
  protected readonly estimate = computed(() => (this.checking() ? null : (this.preflight()?.estimate ?? null)));
  protected readonly duration = computed(() => {
    const estimate = this.estimate();
    return estimate === null ? null : durationRangeText(estimate.serial_seconds_low, estimate.serial_seconds_high);
  });
  protected readonly overCap = computed(() => {
    const estimate = this.estimate();
    return estimate !== null && estimate.total_max > estimate.budget_cap;
  });
  protected readonly ready = computed(() => !this.checking() && this.error() === null && this.blocked() === null && this.preflight() !== null && this.refusals().length === 0);

  protected toggleDetails(): void {
    this.detailsOpen.update((open) => !open);
  }
}
