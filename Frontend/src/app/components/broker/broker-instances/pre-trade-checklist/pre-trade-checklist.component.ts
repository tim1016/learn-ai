import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  effect,
  input,
  signal,
  untracked,
  viewChild,
} from '@angular/core';
import type { LiveInstanceStatus } from '../../../../api/live-instances.types';

import { deriveFleetState } from '../fleet-state';
import { projectFailingGates, type FailingGateRow } from '../failing-gates';

/**
 * Pre-trade checklist — floating action button (FAB) + dialog. Issue #587.
 *
 * The FAB is hidden in STEADY state (nothing to triage), visible otherwise.
 * Clicking the FAB opens a dialog listing each failing readiness gate as a
 * checklist item the operator can acknowledge. Acknowledgement is local
 * UI state — it doesn't change the underlying engine verdict, and a gate
 * that stops failing has its ack pruned, so an old ack can't quietly
 * carry over when the gate breaks again.
 *
 * Renders the same operator-language labels as `<app-can-it-trade-card>`
 * by sharing the parent's gate-labels map and the `projectFailingGates`
 * helper.
 */
@Component({
  selector: 'app-pre-trade-checklist',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './pre-trade-checklist.component.html',
  styleUrl: './pre-trade-checklist.component.scss',
})
export class PreTradeChecklistComponent {
  readonly status = input.required<LiveInstanceStatus>();
  /** Operator-language labels for known gates. Shared with
   * `<app-can-it-trade-card>` so the FAB and the page card never drift. */
  readonly gateLabels = input.required<Record<string, string>>();

  readonly fleetState = computed(() => deriveFleetState(this.status()));
  readonly fabVisible = computed<boolean>(() => this.fleetState() !== 'STEADY');

  readonly failingGates = computed<FailingGateRow[]>(() =>
    projectFailingGates(this.status().readiness, this.gateLabels()),
  );

  private readonly _open = signal(false);
  readonly open = this._open.asReadonly();

  private readonly _acknowledged = signal<ReadonlySet<string>>(new Set<string>());
  readonly acknowledged = this._acknowledged.asReadonly();

  private readonly dialogEl = viewChild<ElementRef<HTMLElement>>('dialogEl');

  constructor() {
    // Prune acks for gates that are no longer failing. If a gate later
    // starts failing again it presents as un-acknowledged.
    effect(() => {
      const failing = new Set(this.failingGates().map((g) => g.key));
      untracked(() => {
        this._acknowledged.update((acks) => {
          if (acks.size === 0) return acks;
          const filtered = new Set([...acks].filter((name) => failing.has(name)));
          return filtered.size === acks.size ? acks : filtered;
        });
      });
    });

    // Focus the dialog when it opens so the scoped (keydown.escape) listener
    // catches Escape without a global document handler.
    effect(() => {
      if (!this._open()) return;
      const el = this.dialogEl();
      if (el) queueMicrotask(() => el.nativeElement.focus());
    });
  }

  toggleOpen(): void {
    this._open.update((v) => !v);
  }

  close(): void {
    this._open.set(false);
  }

  acknowledge(gateKey: string): void {
    this._acknowledged.update((set) => new Set([...set, gateKey]));
  }
}
