import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { InputTextModule } from 'primeng/inputtext';
import { TooltipModule } from 'primeng/tooltip';

import type {
  DeployExecutionMode,
  DeploySizingOption,
} from '../v2-panel/lib/broker-v2-panel.service';

export type DeploySizingPreset = DeploySizingOption['preset'];

const DEPLOY_SIZING_LABELS: Record<DeploySizingPreset, string> = {
  safe_canary: 'One share',
  custom: 'Custom shares',
};

export function deploySizingLabel(preset: DeploySizingPreset): string {
  return DEPLOY_SIZING_LABELS[preset];
}

@Component({
  selector: 'app-deploy-execution-section',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [InputTextModule, TooltipModule],
  templateUrl: './deploy-execution-section.component.html',
  styleUrl: './deploy-execution-section.component.scss',
})
export class DeployExecutionSectionComponent {
  readonly executionModes = input.required<DeployExecutionMode[]>();
  // Backend-authored reason Paper is unreachable for the selected strategy
  // (#1702) — `null` when Paper is admissible or no strategy is selected.
  // Dry Run always admits every runtime-backed row, so only Paper can be
  // disabled by this input; Live stays governed by `availability` alone.
  readonly paperUnavailableReason = input<string | null>(null);
  readonly selectedMode = input.required<DeployExecutionMode['mode']>();
  readonly actionPlanExplanation = input.required<string>();
  readonly symbol = input.required<string>();
  readonly symbolError = input<string | null>(null);
  readonly sizingOptions = input.required<DeploySizingOption[]>();
  readonly sizingPreset = input.required<DeploySizingPreset>();
  readonly quantity = input.required<number>();
  readonly quantityError = input<string | null>(null);
  readonly carryoverAvailable = input.required<boolean>();
  readonly carryoverLabel = input.required<string>();
  readonly carryoverExplanation = input.required<string>();
  readonly carryoverAllowed = input.required<boolean>();

  readonly symbolChange = output<string>();
  readonly symbolBlur = output();
  readonly sizingPresetChange = output<DeploySizingPreset>();
  readonly quantityChange = output<number>();
  readonly quantityBlur = output();
  readonly carryoverAllowedChange = output<boolean>();
  readonly executionModeChange = output<DeployExecutionMode['mode']>();

  protected capabilityStatus(mode: DeployExecutionMode): string {
    if (this.isDisabledForStrategy(mode)) return 'Unavailable';
    return mode.availability === 'available' ? 'Available' : 'Planned';
  }

  protected isPlanned(mode: DeployExecutionMode): boolean {
    return mode.availability === 'planned';
  }

  protected isDisabledForStrategy(mode: DeployExecutionMode): boolean {
    return mode.mode === 'paper' && this.paperUnavailableReason() !== null;
  }

  protected modeExplanation(mode: DeployExecutionMode): string {
    return (this.isDisabledForStrategy(mode) ? this.paperUnavailableReason() : null) ?? mode.explanation;
  }

  protected sizingOptionLabel(preset: DeploySizingPreset): string {
    return deploySizingLabel(preset);
  }

  protected changeSymbol(event: Event): void {
    if (event.target instanceof HTMLInputElement) {
      this.symbolChange.emit(event.target.value);
    }
  }

  protected changeExecutionMode(event: Event): void {
    if (!(event.target instanceof HTMLInputElement)) return;
    const selectedValue = event.target.value;
    const mode = this.executionModes().find(
      (candidate) => candidate.mode === selectedValue,
    );
    if (mode?.availability === 'available' && !this.isDisabledForStrategy(mode)) {
      this.executionModeChange.emit(mode.mode);
    }
  }

  protected changeSizing(event: Event): void {
    if (!(event.target instanceof HTMLInputElement)) return;
    if (event.target.value === 'safe_canary' || event.target.value === 'custom') {
      this.sizingPresetChange.emit(event.target.value);
    }
  }

  protected changeQuantity(event: Event): void {
    if (!(event.target instanceof HTMLInputElement)) return;
    const quantity = Number(event.target.value);
    if (Number.isFinite(quantity)) this.quantityChange.emit(quantity);
  }

  protected changeCarryover(event: Event): void {
    if (event.target instanceof HTMLInputElement) {
      this.carryoverAllowedChange.emit(event.target.checked);
    }
  }
}
