import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { Drawer } from 'primeng/drawer';

import { LeanTwinSourceComponent } from '../../components/strategy-validation/lean-twin-source/lean-twin-source.component';
import { ALWAYS_READY } from '../read-state';
import { SignalStrategySectionComponent } from '../signal-strategy-section.component';
import { SignalStrategyWorkspaceStore } from '../signal-strategy-workspace.store';
import { SignalStrategyApprovedConfigurationsComponent } from './approved-configurations.component';
import { SignalStrategyDeclaredParametersComponent } from './declared-parameters.component';
import { SignalStrategyDescriptionComponent } from './strategy-description.component';

/**
 * What the strategy is, read only. The registry's own description and rules, the
 * source, and the parameters under the four kinds of value: the registry's
 * defaults and the validated corpus point (declared), the values approved for a
 * symbol, and the values currently being explored. The four are never merged,
 * so a default is never mistaken for an approved setting.
 */
@Component({
  selector: 'app-signal-strategy-define',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    Drawer,
    LeanTwinSourceComponent,
    SignalStrategyApprovedConfigurationsComponent,
    SignalStrategyDeclaredParametersComponent,
    SignalStrategyDescriptionComponent,
    SignalStrategySectionComponent,
  ],
  templateUrl: './signal-strategy-define.component.html',
  styles: `
    :host { display: flex; flex-direction: column; gap: var(--space-3); padding: var(--space-3) var(--space-4); }
    .note { margin: 0; color: var(--text-secondary); font-size: var(--fs-sm); }
  `,
})
export class SignalStrategyDefineComponent {
  protected readonly store = inject(SignalStrategyWorkspaceStore);

  protected readonly entry = computed(() => {
    const state = this.store.entry();
    return state.kind === 'found' ? state.entry : null;
  });

  protected readonly always = ALWAYS_READY;
  protected readonly sourceOpen = signal(false);

  constructor() {
    this.store.want('declarations');
    this.store.want('approvals');
  }
}
