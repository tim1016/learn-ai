import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type { DeployBotStrategy } from '../v2-panel/lib/broker-v2-panel.service';

@Component({
  selector: 'app-deploy-evidence-override',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './deploy-evidence-override.component.html',
  styleUrl: './deploy-evidence-override.component.scss',
})
export class DeployEvidenceOverrideComponent {
  readonly strategy = input<DeployBotStrategy | null>(null);
  readonly acknowledged = input(false);
  readonly reason = input('');
  readonly reasonError = input<string | null>(null);

  readonly acknowledgedChange = output<boolean>();
  readonly reasonChange = output<string>();
  readonly reasonBlur = output();

  protected changeAcknowledgement(event: Event): void {
    if (event.target instanceof HTMLInputElement) {
      this.acknowledgedChange.emit(event.target.checked);
    }
  }

  protected changeReason(event: Event): void {
    if (event.target instanceof HTMLTextAreaElement) {
      this.reasonChange.emit(event.target.value);
    }
  }
}
