import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { fmtCurrency, fmtNumber } from '../../../format';

import type { BrokerActivityRow } from '../broker-activity-table/broker-activity.types';

interface WindowContextEntry {
  key: string;
  value: number | string;
}

/**
 * Drill-down drawer rendered under an expanded broker-activity row.
 *
 * Render-only — every visible string comes from the row's structured
 * facts or its backend-authored ``narrative``. The component never
 * composes prose, never invents headers, never derives verdict facts.
 *
 * Sections shown only when their source data is present (frontend may
 * elide an empty section — that's a layout choice, not a derivation).
 */
@Component({
  selector: 'app-broker-activity-row-detail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './broker-activity-row-detail.component.html',
  styleUrl: './broker-activity-row-detail.component.scss',
})
export class BrokerActivityRowDetailComponent {
  readonly row = input.required<BrokerActivityRow>();

  readonly hasEngineOverlay = computed<boolean>(() => this.row().engine_overlay !== null);
  readonly hasDivergenceFacts = computed<boolean>(() => this.row().divergence_facts !== null);

  readonly windowContextEntries = computed<WindowContextEntry[]>(() => {
    const facts = this.row().divergence_facts;
    if (facts === null) return [];
    return Object.entries(facts.window_context).map(([key, value]) => ({
      key,
      value,
    }));
  });

  readonly reasonCodes = computed<readonly string[]>(() => this.row().reason_codes);

  readonly fmtCurrency = fmtCurrency;
  readonly fmtNumber = fmtNumber;

  formatLagMs(ms: number | null | undefined): string {
    if (ms === null || ms === undefined) return '—';
    return `${fmtNumber(ms, 0)} ms`;
  }
}
