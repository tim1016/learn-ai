import { ChangeDetectionStrategy, Component } from '@angular/core';

import { CopyButtonComponent } from '../../../../shared/copy-button/copy-button.component';

@Component({
  selector: 'app-configuration-switch-guide',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CopyButtonComponent],
  templateUrl: './configuration-switch-guide.component.html',
  styleUrl: './configuration-switch-guide.component.scss',
})
export class ConfigurationSwitchGuideComponent {
  protected readonly restartCommand = 'podman compose restart python-service';
}
