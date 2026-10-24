import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type { ReadState } from '../read-state';
import { SignalStrategySectionComponent } from '../signal-strategy-section.component';
import type { QualifiedVersions } from '../signal-strategy-workspace.store';
import { SymbolVersionGroupsComponent } from '../symbol-version-groups.component';

/**
 * Golden Search's qualified versions on Overview, read-only: the shared list, one
 * symbol at a time, with nothing added to a row. A version approves one symbol
 * with one exact configuration, and it is current only while its status says ready.
 */
@Component({
  selector: 'app-signal-strategy-qualified-versions',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [SignalStrategySectionComponent, SymbolVersionGroupsComponent],
  templateUrl: './qualified-versions.component.html',
})
export class SignalStrategyQualifiedVersionsComponent {
  readonly versions = input.required<ReadState<QualifiedVersions>>();
  readonly retry = output();
}
