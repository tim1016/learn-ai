import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { InputTextModule } from 'primeng/inputtext';
import { TooltipModule } from 'primeng/tooltip';

import type { ParamProperty, ParamsSchema } from '../../strategy-lab/strategy-lab.models';

export interface DeployParameterEntry {
  field: string;
  property: ParamProperty;
  isNumeric: boolean;
  currentValue: unknown;
  divergesFromDefault: boolean;
}

/** #1701: schema-driven param form — same `params_schema` shape and
 * per-field rendering idiom Strategy Lab's config rail already uses
 * (`strategy-lab-config-rail.component.ts`), reused here rather than
 * re-invented. `symbol` never appears: the backend's `params_schema`
 * already excludes it (it is deploy-authoritative), not filtered here.
 */
@Component({
  selector: 'app-deploy-parameters-section',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [InputTextModule, TooltipModule],
  templateUrl: './deploy-parameters-section.component.html',
  styleUrl: './deploy-parameters-section.component.scss',
})
export class DeployParametersSectionComponent {
  readonly paramsSchema = input.required<ParamsSchema>();
  readonly values = input.required<Record<string, unknown>>();

  readonly parameterChange = output<{ field: string; value: string | number }>();

  protected readonly entries = computed<DeployParameterEntry[]>(() => {
    const properties = this.paramsSchema().properties ?? {};
    const values = this.values();
    return Object.entries(properties).map(([field, property]) => {
      const isNumeric = property.type === 'integer' || property.type === 'number';
      const currentValue = values[field] ?? property.default;
      return {
        field,
        property,
        isNumeric,
        currentValue,
        divergesFromDefault: String(currentValue) !== String(property.default),
      };
    });
  });

  protected changeParameter(entry: DeployParameterEntry, target: EventTarget | null): void {
    if (!(target instanceof HTMLInputElement)) return;
    const raw = target.value;
    if (entry.isNumeric) {
      const parsed = entry.property.type === 'integer' ? Number.parseInt(raw, 10) : Number.parseFloat(raw);
      if (Number.isFinite(parsed)) this.parameterChange.emit({ field: entry.field, value: parsed });
      return;
    }
    this.parameterChange.emit({ field: entry.field, value: raw });
  }
}
