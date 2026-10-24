import { NgTemplateOutlet } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { formatReceiptLabel } from '../pipes/receipt-label.pipe';
import type { TimestampDisplayMode } from '../timestamp/timestamp-display';
import { TimestampDisplayComponent } from '../timestamp/timestamp-display.component';
import type { DataSourceProvenance, DataSourceWindow } from './data-source-provenance';

/** What a field reads when the server did not record it. */
const NOT_RECORDED = 'not recorded';

type DetailValue =
  | { readonly kind: 'text'; readonly text: string }
  | { readonly kind: 'code'; readonly text: string }
  | { readonly kind: 'window'; readonly bounds: DataSourceWindow }
  | { readonly kind: 'time'; readonly ms: number }
  | { readonly kind: 'list'; readonly items: readonly string[] };

interface DetailRow {
  readonly label: string;
  readonly value: DetailValue;
}

const unrecorded: DetailValue = { kind: 'text', text: NOT_RECORDED };

/** Server-authored text, or "not recorded" when the server gave none. */
function authored(text: string | null): DetailValue {
  return text === null || text === '' ? unrecorded : { kind: 'text', text };
}

/** An identifier the server recorded, worded by `receiptLabel`. */
function identifier(value: string | null): DetailValue {
  return value === null ? unrecorded : authored(formatReceiptLabel(value));
}

function windowValue(bounds: DataSourceWindow | null): DetailValue {
  return bounds === null ? unrecorded : { kind: 'window', bounds };
}

/** The sources as one line, or "not recorded" when the server named none. */
function sourcesText(provenance: DataSourceProvenance): string {
  const sources = provenance.sources.map(formatReceiptLabel).filter((source) => source !== '');
  return sources.length === 0 ? NOT_RECORDED : sources.join(', ');
}

function detailRows(provenance: DataSourceProvenance): readonly DetailRow[] {
  const { counts, dataIdentity, observedAtMs } = provenance;
  const rows: DetailRow[] = [
    { label: 'Sources', value: { kind: 'text', text: sourcesText(provenance) } },
    { label: 'Symbol or universe', value: authored(provenance.subject) },
    { label: 'Requested window', value: windowValue(provenance.requestedWindow) },
    { label: 'Effective window', value: windowValue(provenance.effectiveWindow) },
    { label: 'Warmup', value: authored(provenance.warmup) },
    { label: 'Resolution', value: authored(provenance.resolution) },
    { label: 'Adjustment', value: identifier(provenance.adjustment) },
    { label: 'Session policy', value: identifier(provenance.sessionPolicy) },
    { label: 'Data identity', value: dataIdentity === null || dataIdentity === '' ? unrecorded : { kind: 'code', text: dataIdentity } },
    { label: 'Source data observed', value: observedAtMs === null ? unrecorded : { kind: 'time', ms: observedAtMs } },
    {
      label: 'This request',
      value:
        counts === null
          ? unrecorded
          : { kind: 'text', text: `${counts.added} added · ${counts.reused} reused · ${counts.missing} missing` },
    },
  ];
  if (provenance.limitations.length > 0) {
    rows.push({ label: 'Limitations', value: { kind: 'list', items: provenance.limitations } });
  }
  return rows;
}

/**
 * The data-source line (PRD #3490 R06): a compact projection of the server's
 * provenance for a result, with every recorded fact one disclosure away.
 *
 * The line names the sources, the window actually read, the resolution and the
 * adjustment, and raises what the reader should not miss: missing data and
 * stated limitations. The details list every field the server can record and
 * read "not recorded" for each one it did not.
 *
 * A legacy record without provenance is `null`, and the line says so in one
 * sentence. Nothing is inferred from current holdings or defaults.
 *
 * Distinct from `app-data-source`, the free-text caption for live feeds: this
 * renders a typed, server-supplied record.
 */
@Component({
  selector: 'app-workspace-data-source',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgTemplateOutlet, TimestampDisplayComponent],
  templateUrl: './workspace-data-source.component.html',
  styleUrl: './workspace-data-source.component.scss',
})
export class WorkspaceDataSourceComponent {
  /** The server's provenance, or `null` for a record that has none. */
  readonly provenance = input<DataSourceProvenance | null>(null);

  /**
   * How a window's bounds are written. `et` is the exchange-aligned instant
   * with its marker. `date-et` is for a window whose ends are dates (a daily
   * study): the ET calendar date, never shifted a day. The host knows which
   * its endpoint anchors.
   */
  readonly windowMode = input<Extract<TimestampDisplayMode, 'et' | 'date-et'>>('et');

  protected readonly NOT_RECORDED = NOT_RECORDED;

  /** What the collapsed line says beyond the window: sources first, then only the facts worth a glance. */
  protected readonly summary = computed(() => {
    const provenance = this.provenance();
    if (provenance === null) return null;
    const adjustment = provenance.adjustment === null ? '' : formatReceiptLabel(provenance.adjustment);
    return {
      sources: sourcesText(provenance),
      bounds: provenance.effectiveWindow,
      resolution: provenance.resolution ?? '',
      adjustment,
      missing: provenance.counts !== null && provenance.counts.missing > 0 ? provenance.counts.missing : 0,
      limitations: provenance.limitations.length,
    };
  });

  protected readonly rows = computed(() => {
    const provenance = this.provenance();
    return provenance === null ? [] : detailRows(provenance);
  });
}
