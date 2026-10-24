import { LowerCasePipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type { ParamProperty } from '../../components/strategy-lab/strategy-lab.models';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import type { ReadState } from '../read-state';
import { allowedRangeText, valueText, type StrategyRegistryEntry } from '../signal-strategy-evidence';
import { SignalStrategySectionComponent } from '../signal-strategy-section.component';
import type { StrategyDeclarations } from '../signal-strategy-workspace.store';

/** What a parameter's row says where a declaration has nothing to say. */
const NOT_DECLARED = 'not declared';

interface ParameterRow {
  readonly name: string;
  readonly title: string | null;
  readonly description: string | null;
  readonly unit: string | null;
  readonly allowed: string;
  readonly searchRange: string;
  readonly registryDefault: string;
  readonly validated: string;
}

/**
 * The strategy's declared parameters. Three of the four kinds of value live
 * here, each in its own column and never merged: what the registry's schema
 * defaults to, what the program was validated at, and the range Python allows.
 * The ranges and constraints are Python's: the schema's bounds and Golden
 * Search's declaration. A column with nothing declared says so.
 */
@Component({
  selector: 'app-signal-strategy-declared-parameters',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [LowerCasePipe, ReceiptLabelPipe, SignalStrategySectionComponent],
  templateUrl: './declared-parameters.component.html',
  styleUrl: '../section-content.scss',
})
export class SignalStrategyDeclaredParametersComponent {
  readonly entry = input.required<StrategyRegistryEntry>();
  readonly declarations = input.required<ReadState<StrategyDeclarations>>();
  readonly retry = output();

  protected readonly rows = computed<readonly ParameterRow[]>(() => {
    const state = this.declarations();
    const schema = state.kind === 'ready' ? state.value.schema : null;
    if (state.kind !== 'ready' || schema === null) return [];
    const knobs = new Map((state.value.golden?.knobs ?? []).map((knob) => [knob.name, knob]));
    const { validated_settings: validated, parameter_units: units } = this.entry();
    return Object.entries<ParamProperty>(schema.params_schema.properties ?? {}).map(([name, property]) => {
      const knob = knobs.get(name);
      return {
        name,
        title: property.title ?? null,
        description: property.description ?? null,
        unit: units[name] ?? null,
        allowed: allowedRangeText(property),
        searchRange:
          knob !== undefined
            ? `${knob.domain_low} to ${knob.domain_high}, step ${knob.quantum}`
            : state.value.golden === null ? NOT_DECLARED : 'not a search parameter',
        registryDefault: property.default === undefined ? NOT_DECLARED : valueText(property.default),
        validated: name in validated ? valueText(validated[name]) : NOT_DECLARED,
      };
    });
  });

  protected readonly constraints = computed(() => {
    const state = this.declarations();
    return state.kind === 'ready' ? (state.value.golden?.constraints ?? []) : [];
  });

  /** The strategy is not in Strategy Lab's catalog, so Python declares no parameter schema to show. */
  protected readonly noSchema = computed(() => {
    const state = this.declarations();
    return state.kind === 'ready' && state.value.schema === null;
  });
}
