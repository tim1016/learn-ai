import { ChangeDetectionStrategy, Component, computed, inject, resource, signal } from '@angular/core';
import type { LiveInstanceSummary } from '../../../api/live-instances.types';
import { LiveRunsService } from '../../../services/live-runs.service';

/**
 * Instance control room — foundation (ADR 0004).
 *
 * The console's subject is the strategy instance; the current run and its
 * artifacts are attached as evidence. This minimal view stands up the
 * instance spine (fleet list -> instance status with live-vs-evidence binding);
 * the full re-spine and operator panels land with the cutover slice.
 */
@Component({
  selector: 'app-broker-instances',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './broker-instances.component.html',
  styleUrl: './broker-instances.component.scss',
})
export class BrokerInstancesComponent {
  private readonly svc = inject(LiveRunsService);

  readonly selectedInstanceId = signal<string | null>(null);

  readonly fleet = resource({
    loader: () => this.svc.getInstances(),
  });

  readonly status = resource({
    params: () => this.selectedInstanceId() ?? undefined,
    loader: ({ params }) => this.svc.getInstanceStatus(params),
  });

  readonly instances = computed<LiveInstanceSummary[]>(() => this.fleet.value() ?? []);

  select(instanceId: string): void {
    this.selectedInstanceId.set(instanceId);
  }
}
