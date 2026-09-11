import { HttpClient } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, inject, resource } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { CopyButtonComponent } from '../../../../shared/copy-button/copy-button.component';

/**
 * Read-only presentation of the repo-owned Paper-to-Live operator script.
 *
 * The shell asset is the executable source of truth: this component fetches
 * and copies those exact bytes instead of maintaining a second template.
 */
@Component({
  selector: 'app-configuration-handoff-script',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CopyButtonComponent],
  templateUrl: './configuration-handoff-script.component.html',
  styleUrl: './configuration-handoff-script.component.scss',
})
export class ConfigurationHandoffScriptComponent {
  private readonly http = inject(HttpClient);
  protected readonly scriptUrl = '/assets/scripts/alpaca-paper-to-live-handoff.sh';
  protected readonly script = resource({
    loader: () => firstValueFrom(this.http.get(this.scriptUrl, { responseType: 'text' })),
  });
}
