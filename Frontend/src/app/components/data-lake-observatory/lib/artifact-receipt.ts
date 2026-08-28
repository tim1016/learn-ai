import { isOpaqueReceiptValueLabel } from '../../../shared/pipes/receipt-label.pipe';
import type { ArtifactDetail } from './data-lake.types';

/**
 * One line of an artifact receipt, tagged with how it must be rendered.
 *
 * The split is the repo's receipt rule made explicit: a backend identifier
 * (`code`) goes through the receipt-label pipe; an opaque audit token
 * (`exact` — hashes, paths, ids) is reproduced verbatim; a date-anchored
 * trading date (`date`) renders in ET and an instant (`instant`) in the
 * viewer's zone.
 */
export type ReceiptRow =
  | { readonly kind: 'code'; readonly label: string; readonly value: string }
  | { readonly kind: 'exact'; readonly label: string; readonly value: string }
  | { readonly kind: 'date'; readonly label: string; readonly value: number }
  | { readonly kind: 'instant'; readonly label: string; readonly value: number }
  | { readonly kind: 'count'; readonly label: string; readonly value: number }
  | { readonly kind: 'prose'; readonly label: string; readonly value: string };

export interface ReceiptSection {
  readonly title: string;
  readonly rows: readonly ReceiptRow[];
}

const BYTE_UNITS = ['B', 'KB', 'MB', 'GB', 'TB'] as const;

/** Human byte size for a receipt line. Exact byte count stays alongside it. */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const rounded = unit === 0 ? String(value) : value.toFixed(value < 10 ? 2 : 1);
  return `${rounded} ${BYTE_UNITS[unit]}`;
}

function code(label: string, value: string | null): ReceiptRow[] {
  return value === null || value === '' ? [] : [{ kind: 'code', label, value }];
}

function exact(label: string, value: string | null): ReceiptRow[] {
  return value === null || value === '' ? [] : [{ kind: 'exact', label, value }];
}

function instant(label: string, value: number | null): ReceiptRow[] {
  return value === null ? [] : [{ kind: 'instant', label, value }];
}

function count(label: string, value: number | null): ReceiptRow[] {
  return value === null ? [] : [{ kind: 'count', label, value }];
}

/**
 * Renders one `provider_params` entry.
 *
 * Scalars follow the shared opaque-label rule (`…_id`, `…_hash`, `…_path`,
 * `…_ref`, `…_url` stay verbatim); anything structured is reproduced as
 * JSON rather than flattened, because a provider parameter is part of the
 * artifact's provenance and must survive the round trip unaltered.
 */
function providerParamRow(key: string, value: unknown): ReceiptRow {
  if (value === null || typeof value === 'object') {
    return { kind: 'exact', label: key, value: JSON.stringify(value) };
  }
  const text = String(value);
  return isOpaqueReceiptValueLabel(key)
    ? { kind: 'exact', label: key, value: text }
    : { kind: 'code', label: key, value: text };
}

/** Groups one catalog row into the sections the inspector drawer renders. */
export function artifactReceiptSections(detail: ArtifactDetail): readonly ReceiptSection[] {
  const identity: ReceiptRow[] = [
    { kind: 'exact', label: 'artifact_id', value: String(detail.id) },
    { kind: 'code', label: 'artifact_kind', value: detail.artifact_kind },
    { kind: 'code', label: 'status', value: detail.status },
    // `market` and `symbol` are catalog values, not vocabulary: the pipe's
    // title-casing turns "SPY" into "Spy" and "usa" into "Usa", neither of
    // which is what the catalog holds. Every other surface in this feature
    // (heatmap row header, storage-summary table, this panel's own eyebrow)
    // already renders them raw; the receipt must agree with them.
    ...exact('market', detail.market),
    ...exact('symbol', detail.symbol),
    ...(detail.trading_date_ms === null
      ? []
      : [{ kind: 'date' as const, label: 'trading_date', value: detail.trading_date_ms }]),
    ...code('resolution', detail.resolution),
    ...code('data_type', detail.data_type),
    ...code('provider', detail.provider),
    ...code('price_adjustment_mode', detail.price_adjustment_mode),
  ];

  const integrity: ReceiptRow[] = [
    { kind: 'exact', label: 'data_contract_hash', value: detail.data_contract_hash },
    ...(detail.content_hash === null
      ? [
          {
            kind: 'prose' as const,
            label: 'content_hash',
            value: 'Not recorded until the artifact reaches complete.',
          },
        ]
      : [{ kind: 'exact' as const, label: 'content_hash', value: detail.content_hash }]),
    { kind: 'exact', label: 'file_path', value: detail.file_path },
    ...(detail.file_size_bytes === null
      ? []
      : [
          {
            kind: 'prose' as const,
            label: 'file_size_bytes',
            value: `${formatBytes(detail.file_size_bytes)} (${detail.file_size_bytes} bytes)`,
          },
        ]),
    ...count('row_count', detail.row_count),
  ];

  const timeline: ReceiptRow[] = [
    ...instant('first_bar_start', detail.first_bar_start_ms),
    ...instant('last_bar_start', detail.last_bar_start_ms),
    { kind: 'instant', label: 'fetched_at', value: detail.fetched_at_ms },
    ...instant('completed_at', detail.completed_at_ms),
  ];

  const diagnostics: ReceiptRow[] = [
    { kind: 'count', label: 'attempt_count', value: detail.attempt_count },
    ...code('last_error', detail.last_error),
    ...(detail.error_message === null || detail.error_message === ''
      ? []
      : [{ kind: 'prose' as const, label: 'error_message', value: detail.error_message }]),
  ];

  const provenance = Object.entries(detail.provider_params).map(([key, value]) =>
    providerParamRow(key, value),
  );

  return [
    { title: 'Identity', rows: identity },
    { title: 'Integrity', rows: integrity },
    { title: 'Timeline', rows: timeline },
    { title: 'Provider parameters', rows: provenance },
    { title: 'Diagnostics', rows: diagnostics },
  ].filter((section) => section.rows.length > 0);
}
