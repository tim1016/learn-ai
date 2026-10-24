import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';

import { ALWAYS_READY } from '../read-state';
import { SignalStrategyCapabilitiesComponent } from '../signal-strategy-capabilities.component';
import { SignalStrategySectionComponent } from '../signal-strategy-section.component';
import { SignalStrategyWorkspaceStore } from '../signal-strategy-workspace.store';
import { SignalStrategyConfigurationEvidenceComponent } from './configuration-evidence.component';
import { SignalStrategyProgramProofComponent } from './program-proof.component';
import { SignalStrategyQualifiedVersionsComponent } from './qualified-versions.component';
import { SignalStrategyRunningBotsComponent } from './running-bots.component';

/**
 * What is known about one strategy, each fact under its own scope: what the
 * registry allows, the program's proof, the configurations reviewed and
 * qualified for single symbols, and the bots running now. There is no
 * strategy-wide approval here: nothing sums one symbol's evidence into a
 * verdict on the strategy.
 */
@Component({
  selector: 'app-signal-strategy-overview',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    SignalStrategyCapabilitiesComponent,
    SignalStrategyConfigurationEvidenceComponent,
    SignalStrategyProgramProofComponent,
    SignalStrategyQualifiedVersionsComponent,
    SignalStrategyRunningBotsComponent,
    SignalStrategySectionComponent,
  ],
  templateUrl: './signal-strategy-overview.component.html',
  styles: `
    :host { display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-3) var(--space-4); }
  `,
})
export class SignalStrategyOverviewComponent {
  protected readonly store = inject(SignalStrategyWorkspaceStore);

  protected readonly entry = computed(() => {
    const state = this.store.entry();
    return state.kind === 'found' ? state.entry : null;
  });

  protected readonly always = ALWAYS_READY;

  constructor() {
    this.store.want('approvals');
    this.store.want('fleet');
  }
}
