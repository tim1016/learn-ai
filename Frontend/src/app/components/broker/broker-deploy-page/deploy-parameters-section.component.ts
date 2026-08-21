import { ChangeDetectionStrategy, Component, computed, effect, input, output, signal } from '@angular/core';
import { InputTextModule } from 'primeng/inputtext';
import { TooltipModule } from 'primeng/tooltip';

import type {
  DeployStrategyParamProperty,
  DeployStrategyParamsSchema,
} from '../v2-panel/lib/broker-v2-panel.service';

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
  imports: [InputTextModule, TooltipModule],
  templateUrl: './deploy-parameters-section.component.html',
  styleUrl: './deploy-parameters-section.component.scss',
})
export class DeployParametersSectionComponent {
  readonly paramsSchema = input.required<DeployStrategyParamsSchema>();
  readonly values = input.required<Record<string, unknown>>();

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
