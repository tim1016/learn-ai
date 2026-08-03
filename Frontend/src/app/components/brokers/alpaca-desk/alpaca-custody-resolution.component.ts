import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';

import type { CustodyDivergence, CustodyPositionDelta } from '../../../api/alpaca.types';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { BrokersService } from '../../../services/brokers.service';

/**
 * Alpaca custody-resolution card (Slice 1, read-only). Renders the
 * backend-authored Clerk↔broker custody diagnosis. Four distinct renders:
 * loading, error, in-sync, and diverged. The diverged state surfaces each
 * divergence's explanation, position deltas, and possible causes verbatim.
 * The "Resolve & sync" action is disabled here — Slice 2 (Task 2.4) wires the
 * mutating resolve flow; this slice never calls a resolve endpoint.
 */
@Component({
  selector: 'app-alpaca-custody-resolution',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe, TimestampDisplayComponent],
  templateUrl: './alpaca-custody-resolution.component.html',
  styleUrl: './alpaca-custody-resolution.component.scss',
  host: { class: 'block' },
})
export class AlpacaCustodyResolutionComponent {
  private readonly brokers = inject(BrokersService);

  protected readonly diagnosis = resource({
    loader: () => this.brokers.getCustodyDiagnosis('alpaca'),
  });

  // `divergences` is optional (`?:`) on the generated CustodyDiagnosis schema
  // (default `[]` server-side); normalize here so the template never has to
  // reason about `undefined`.
  protected readonly divergences = computed<CustodyDivergence[]>(
    () => this.diagnosis.value()?.divergences ?? [],
  );

  protected positionDeltas(divergence: CustodyDivergence): CustodyPositionDelta[] {
    return divergence.position_deltas ?? [];
  }
}
