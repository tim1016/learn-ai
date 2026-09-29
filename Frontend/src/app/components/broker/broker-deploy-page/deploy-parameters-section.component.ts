import { ChangeDetectionStrategy, Component, computed, effect, input, output, signal, viewChild, untracked } from '@angular/core';
import { TooltipModule } from 'primeng/tooltip';

import type {
  DeployStrategyParamProperty,
  QualifiedDeployConfiguration,
  DeployStrategyParamsSchema,
} from '../v2-panel/lib/broker-v2-panel.service';

import { InstrumentCardComponent } from '../../../shared/ticker-range-picker/parts/instrument-card.component';
import { AssetIdentityComponent } from '../../../shared/asset-identity';
import type { TickerRange } from '../../../shared/ticker-range-picker/ticker-range-picker.types';

export interface DeployParameterEntry {
  field: string;
  property: DeployStrategyParamProperty;
  isNumeric: boolean;
  currentValue: unknown;
  divergesFromDefault: boolean;
  invalid: boolean;
}

function parseStrictNumber(raw: string, type: string | null | undefined): number | null {
  const trimmed = raw.trim();
  if (trimmed === '') return null;
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed)) return null;
  if (type === 'integer' && !Number.isInteger(parsed)) return null;
  return parsed;
}

/** #1701: schema-driven param form — same rendering idiom Strategy Lab's
 * config rail uses (`strategy-lab-config-rail.component.ts`), but built on
 * this page's own OpenAPI-generated schema types rather than Strategy Lab's
 * feature-local ones — the two hosts use different form idioms (this page
 * uses `@angular/forms/signals` elsewhere; Strategy Lab's rail uses plain
 * template inputs/outputs), so the component is not literally shared.
 * `symbol` never appears: the backend's `params_schema` already excludes it
 * (it is deploy-authoritative), not filtered here.
 */
@Component({
  selector: 'app-deploy-parameters-section',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TooltipModule, InstrumentCardComponent, AssetIdentityComponent],
  templateUrl: './deploy-parameters-section.component.html',
  styleUrl: './deploy-parameters-section.component.scss',
})
export class DeployParametersSectionComponent {
  readonly qualifiedConfiguration = input<QualifiedDeployConfiguration | null>(null);
  readonly qualifiedConfigurationSelected = output<QualifiedDeployConfiguration>();
  readonly dryRunRequested = output();
  protected readonly qualifiedPicker = viewChild<InstrumentCardComponent>('qualifiedPicker');
  // The raw `app-instrument-card` rather than `app-symbol-picker`: this host
  // needs the card's `tickerPool()` lookup and coverage-gate controls, which
  // the picker wrapper does not expose. Membership and backfill gating stay
  // inside the shared card (ADR 0066); this value is only the picker's
  // required shape and never a hand-rolled symbol suggestion.
  protected readonly qualifiedPickerValue: TickerRange = { symbol: '', from: '', to: '', resolution: 'daily' };
  protected readonly coverageDetailsOpen = signal(false);
  private pendingQualified: QualifiedDeployConfiguration | null = null;
  protected readonly qualifiedRow = computed(() => {
    const symbol = this.qualifiedConfiguration()?.symbol;
    return this.qualifiedPicker()?.tickerPool().find(row => row.symbol === symbol) ?? null;
  });
  protected readonly qualifiedParameters = computed(() => Object.entries(this.qualifiedConfiguration()?.parameters ?? {}));

  protected useQualifiedConfiguration(): void {
    const configuration = this.qualifiedConfiguration();
    const row = this.qualifiedRow();
    if (!configuration || !row) return;
    this.pendingQualified = configuration;
    this.coverageDetailsOpen.set(true);
    // The shared card owns joined membership and every lake/backfill check.
    this.qualifiedPicker()?.pickTicker(row);
  }

  protected qualifiedSymbolSelected(symbol: string): void {
    const selected = this.pendingQualified;
    this.pendingQualified = null;
    this.coverageDetailsOpen.set(false);
    if (selected && selected === this.qualifiedConfiguration() && symbol === selected.symbol) {
      this.invalidFieldsRaw.set(new Set());
      this.qualifiedConfigurationSelected.emit(selected);
    }
  }

  protected requestDryRun(): void {
    this.pendingQualified = null;
    this.qualifiedPicker()?.gate.abandon();
    this.coverageDetailsOpen.set(false);
    this.dryRunRequested.emit();
  }

  readonly paramsSchema = input.required<DeployStrategyParamsSchema>();
  readonly values = input.required<Readonly<Record<string, unknown>>>();
  /** Whether other values may be typed. A Golden-scoped strategy trades only
   * its qualified settings in a broker world; Dry Run opens the editor. */
  readonly editable = input(true);

  readonly parameterChange = output<{ field: string; value: string | number }>();
  /** Emits the current set of fields whose displayed text does not parse to
   * a valid value, every time that set changes. The host blocks deployment
   * while it is non-empty (#1709 review finding 3): a field showing invalid
   * or blank text must never silently submit its last-known-good value. */
  readonly invalidFieldsChange = output<ReadonlySet<string>>();

  // Raw, unfiltered set of fields the user has left showing unparseable
  // text. Filtered against the *current* schema in `currentInvalidFields`
  // so a stale entry from a since-abandoned strategy selection can never
  // leave the host permanently blocked.
  private readonly invalidFieldsRaw = signal<ReadonlySet<string>>(new Set());

  protected readonly currentInvalidFields = computed<ReadonlySet<string>>(() => {
    const schemaFields = new Set(Object.keys(this.paramsSchema().properties ?? {}));
    return new Set([...this.invalidFieldsRaw()].filter((field) => schemaFields.has(field)));
  });

  constructor() {
    effect(() => {
      this.qualifiedConfiguration();
      this.values();
      this.pendingQualified = null;
      const picker = this.qualifiedPicker();
      untracked(() => picker?.gate.abandon());
    });
    effect(() => this.invalidFieldsChange.emit(this.currentInvalidFields()));
  }

  protected readonly entries = computed<DeployParameterEntry[]>(() => {
    const properties = this.paramsSchema().properties ?? {};
    const values = this.values();
    const invalid = this.currentInvalidFields();
    return Object.entries(properties).map(([field, property]) => {
      const isNumeric = property.type === 'integer' || property.type === 'number';
      const currentValue = values[field] ?? property.default;
      return {
        field,
        property,
        isNumeric,
        currentValue,
        divergesFromDefault: String(currentValue) !== String(property.default),
        invalid: invalid.has(field),
      };
    });
  });

  protected changeParameter(entry: DeployParameterEntry, target: EventTarget | null): void {
    if (!(target instanceof HTMLInputElement)) return;
    const raw = target.value;
    const value = entry.isNumeric ? parseStrictNumber(raw, entry.property.type) : (raw.trim() === '' ? null : raw);
    if (value === null) {
      this.markInvalid(entry.field, true);
      return;
    }
    this.markInvalid(entry.field, false);
    this.parameterChange.emit({ field: entry.field, value });
  }

  private markInvalid(field: string, invalid: boolean): void {
    const current = this.invalidFieldsRaw();
    const isInvalid = current.has(field);
    if (invalid === isInvalid) return;
    const next = new Set(current);
    if (invalid) next.add(field);
    else next.delete(field);
    this.invalidFieldsRaw.set(next);
  }
}
