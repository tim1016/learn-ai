import { ChangeDetectionStrategy, Component, computed, inject, resource } from '@angular/core';

import type { CustodyDivergence } from '../../../api/alpaca.types';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { BrokersService } from '../../../services/brokers.service';
import { CustodyDivergenceComponent } from './custody-divergence.component';

/**
 * Alpaca custody-resolution card. Slice 1 (read-only) renders the
 * backend-authored Clerk↔broker diagnosis. This card is deliberately read-only:
 * all custody mutations are exact actions owned by the SQLite projection.
 */
@Component({
  selector: 'app-alpaca-custody-resolution',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    TimestampDisplayComponent,
    CustodyDivergenceComponent,
  ],
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
}
