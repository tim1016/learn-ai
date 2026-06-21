import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { DatePipe } from '@angular/common';

import type { SizingAuditRow } from '../../../../../api/live-instances.types';

/**
 * Per-trade sizing audit table (PRD #607 / Slice 6 / #613).
 *
 * Columns map to the actual ``SizingAuditRow`` fields (the
 * previously-planned ``INTENT`` / ``SIDE`` / ``VERDICT`` columns
 * don't exist on the Python DTO — this slice does NOT invent fields
 * that don't exist server-side).
 *
 * Renders nothing when ``rows`` is empty (legacy / pre-policy runs);
 * no empty-state placeholder.
 */
@Component({
  selector: 'app-sizing-audit-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DatePipe],
  templateUrl: './sizing-audit-table.component.html',
  styleUrl: './sizing-audit-table.component.scss',
})
export class SizingAuditTableComponent {
  readonly rows = input.required<SizingAuditRow[]>();

  readonly hasRows = computed<boolean>(() => this.rows().length > 0);

  provenanceBadge(provenance: string | null | undefined): string {
    switch (provenance) {
      case 'reference_native':
        return 'reference native';
      case 'live_override':
        return 'live override';
      case 'spec_default':
        return 'spec default';
      default:
        return 'unknown';
    }
  }

  resultLabel(row: SizingAuditRow): string {
    if (row.skipped === true) {
      return `Skipped: ${row.skip_reason ?? 'unknown'}`;
    }
    return 'Filled';
  }
}
