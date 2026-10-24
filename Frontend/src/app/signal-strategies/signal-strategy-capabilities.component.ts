import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { SignalStrategyChipComponent } from './signal-strategy-chip.component';
import type { StrategyRegistryEntry } from './signal-strategy-evidence';

type Capabilities = StrategyRegistryEntry['capabilities'];

/** The registry's three actions, in the order a reader meets them, named as the menu names them. */
const ACTIONS = [
  { id: 'sweep', label: 'Sweep' },
  { id: 'golden', label: 'Golden Search' },
  { id: 'live', label: 'Live' },
] as const satisfies readonly { readonly id: keyof Capabilities; readonly label: string }[];

/**
 * What the registry allows for one strategy, with Python's reason beside each
 * action it does not. Allowed only means the registry does not rule the action
 * out: proof, qualification and admission are judged elsewhere, so an allowed
 * action is never drawn as a good sign.
 */
@Component({
  selector: 'app-signal-strategy-capabilities',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [SignalStrategyChipComponent],
  template: `
    <ul class="actions">
      @for (action of actions(); track action.id) {
        <li>
          <app-signal-strategy-chip [tone]="action.supported ? null : 'degraded'">
            {{ action.label }}: {{ action.supported ? 'allowed' : 'not allowed' }}
          </app-signal-strategy-chip>
          @if (action.reason; as reason) {
            <span class="reason">{{ reason }}</span>
          }
        </li>
      }
    </ul>
  `,
  styles: `
    :host { display: block; min-width: 0; }

    .actions {
      display: flex;
      flex-direction: column;
      gap: var(--space-1);
      margin: 0;
      padding: 0;
      list-style: none;
      font-size: var(--fs-sm);
    }

    .actions li { display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--space-1) var(--space-2); }
    .reason { color: var(--text-secondary); font-size: var(--fs-xs); }
  `,
})
export class SignalStrategyCapabilitiesComponent {
  readonly capabilities = input.required<Capabilities>();

  protected readonly actions = computed(() =>
    ACTIONS.map(({ id, label }) => ({ id, label, supported: this.capabilities()[id].supported, reason: this.capabilities()[id].reason })),
  );
}
