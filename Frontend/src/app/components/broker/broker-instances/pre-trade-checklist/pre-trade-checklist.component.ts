import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  input,
  signal,
  untracked,
} from '@angular/core';
import type { LiveInstanceStatus } from '../../../../api/live-instances.types';

import { deriveFleetState } from '../fleet-state';
import { projectFailingGates, type FailingGateRow } from '../failing-gates';
import { ChecklistDialogComponent } from './checklist-dialog.component';
import { ChecklistFabComponent } from './checklist-fab.component';

/**
 * Pre-trade checklist orchestrator (PRD #607 / Slice 7 / #614).
 *
 * Owns ``open`` state + ``acknowledged`` set + failing-gate
 * computation; places the FAB and (conditionally) the dialog as
 * sibling child components.  Public surface (selector + inputs) is
 * preserved so existing parent template usage is unaffected.
 *
 * Focus restoration: when the dialog closes the orchestrator bumps
 * ``restoreFocusTick`` so the FAB grabs focus back — keyboard
 * operators are not stranded.
 */
@Component({
  selector: 'app-pre-trade-checklist',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ChecklistFabComponent, ChecklistDialogComponent],
  template: `
    <app-checklist-fab
      [visible]="fabVisible()"
      [failingCount]="failingGates().length"
      [open]="open()"
      [restoreFocusTick]="restoreFocusTick()"
      (toggleRequested)="toggleOpen()"
    />
    <app-checklist-dialog
      [open]="open()"
      [failingGates]="failingGates()"
      [acknowledged]="acknowledged()"
      (acknowledge)="acknowledge($event)"
      (closeRequested)="close()"
    />
  `,
})
export class PreTradeChecklistComponent {
  readonly status = input.required<LiveInstanceStatus>();
  /** Operator-language labels for known gates. Shared with
   * ``<app-can-it-trade-card>`` so the FAB and the page card never drift. */
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

  private readonly _restoreFocusTick = signal<number>(0);
  readonly restoreFocusTick = this._restoreFocusTick.asReadonly();

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
  }

  toggleOpen(): void {
    this._open.update((v) => !v);
    if (!this._open()) {
      this.bumpRestoreFocus();
    }
  }

  close(): void {
    if (this._open()) {
      this._open.set(false);
      this.bumpRestoreFocus();
    }
  }

  acknowledge(gateKey: string): void {
    this._acknowledged.update((set) => new Set([...set, gateKey]));
  }

  private bumpRestoreFocus(): void {
    this._restoreFocusTick.update((n) => n + 1);
  }
}
