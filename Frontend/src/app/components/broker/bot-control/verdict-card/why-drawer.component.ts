import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  effect,
  input,
  output,
  viewChild,
} from '@angular/core';

import type {
  OperatorGate,
  OperatorSurfaceTraderGuidance,
} from '../../../../api/live-instances.types';
import type { Disposition, OperatorBlocker } from '../../../../api/operator-blocker.types';
import { fmtTimestampLocal } from '../../format';
import {
  formatReceiptLabel,
  formatReceiptValue,
} from '../../../../shared/pipes/receipt-label.pipe';

interface DrawerEvidenceRow {
  readonly label: string;
  readonly value: string;
  readonly meta: string | null;
}

interface DrawerBlockerGroup {
  readonly disposition: Disposition;
  readonly label: string;
  readonly blockers: readonly OperatorBlocker[];
}

const BLOCKER_GROUP_LABEL: Record<Disposition, string> = {
  terminal: "Can't recover",
  fix_here: 'Fix on this screen',
  fix_elsewhere: 'Fix elsewhere',
  wait: 'Waiting',
};

const BLOCKER_GROUP_ORDER: readonly Disposition[] = [
  'terminal',
  'fix_here',
  'fix_elsewhere',
  'wait',
];

/** Scoped "why?" drawer (spec §Why-drawer contract). Renders only the receipts
 *  behind the state's current verdict: the trader-guidance claim, its proof
 *  lines, the failing readiness gates, and advanced evidence facts — all through
 *  the shared `receiptLabel` pipe. It NEVER introduces a new action; the only
 *  control is Close. Empty is honest, never "Unknown". */
@Component({
  selector: 'app-why-drawer',
  templateUrl: './why-drawer.component.html',
  styleUrl: './why-drawer.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { '(keydown.escape)': 'onEscape()' },
})
export class WhyDrawerComponent {
  readonly open = input.required<boolean>();
  readonly guidance = input<OperatorSurfaceTraderGuidance | null>(null);
  readonly gates = input<readonly OperatorGate[]>([]);
  readonly blockers = input<readonly OperatorBlocker[]>([]);

  readonly closed = output();

  private readonly closeButton = viewChild<ElementRef<HTMLButtonElement>>('closeButton');

  readonly headline = computed(() => this.guidance()?.headline ?? null);
  readonly explanation = computed(() => this.guidance()?.explanation ?? null);
  readonly proofLines = computed(() => this.guidance()?.proof_lines ?? []);

  readonly failingGates = computed(() =>
    this.gates().filter((gate) => gate.gate_result.status !== 'pass'),
  );
  readonly blockerGroups = computed<DrawerBlockerGroup[]>(() => {
    const blockers = this.blockers();
    return BLOCKER_GROUP_ORDER
      .map((disposition) => ({
        disposition,
        label: BLOCKER_GROUP_LABEL[disposition],
        blockers: blockers.filter((blocker) => blocker.disposition === disposition),
      }))
      .filter((group) => group.blockers.length > 0);
  });

  readonly evidence = computed<DrawerEvidenceRow[]>(() => {
    const facts = this.guidance()?.advanced_evidence ?? [];
    return facts.map((fact) => ({
      label: formatReceiptLabel(fact.label),
      value: formatReceiptValue(fact.label, fact.value),
      meta: fact.ts_ms !== null && fact.ts_ms_resolved ? fmtTimestampLocal(fact.ts_ms) : null,
    }));
  });

  readonly hasContent = computed(
    () =>
      this.headline() !== null ||
      this.blockerGroups().length > 0 ||
      this.proofLines().length > 0 ||
      this.failingGates().length > 0 ||
      this.evidence().length > 0,
  );

  constructor() {
    effect(() => {
      if (this.open()) {
        queueMicrotask(() => this.closeButton()?.nativeElement.focus());
      }
    });
  }

  gateLabel(gate: OperatorGate): string {
    return formatReceiptLabel(gate.name);
  }

  onEscape(): void {
    if (this.open()) this.close();
  }

  close(): void {
    this.closed.emit();
  }
}
