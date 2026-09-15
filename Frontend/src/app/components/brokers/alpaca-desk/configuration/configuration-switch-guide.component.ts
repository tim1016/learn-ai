import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { CopyButtonComponent } from '../../../../shared/copy-button/copy-button.component';

@Component({
  selector: 'app-configuration-switch-guide',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CopyButtonComponent],
  templateUrl: './configuration-switch-guide.component.html',
  styleUrl: './configuration-switch-guide.component.scss',
})
export class ConfigurationSwitchGuideComponent {
  /**
   * The restart command the backend authored for this lane's own worker, or
   * `null` when the deployment declared no worker service. Never composed
   * here: only the deployment knows which compose service runs this worker,
   * and the fleet coordinator's name applies no lane's staged profile.
   */
  readonly restartCommand = input.required<string | null>();

  protected readonly noWorkerServiceDeclared =
    "This deployment did not declare this lane's worker service. Restart the lane's worker "
    + 'container from the host, then continue.';
}
