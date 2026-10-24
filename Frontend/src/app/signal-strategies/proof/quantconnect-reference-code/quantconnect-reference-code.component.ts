import { ChangeDetectionStrategy, Component, input } from '@angular/core';

import { CopyButtonComponent } from '../../../shared/copy-button/copy-button.component';
import type { StrategyReferenceCode } from '../../../services/strategy-validation.types';

@Component({
  selector: 'app-quantconnect-reference-code',
  imports: [CopyButtonComponent],
  templateUrl: './quantconnect-reference-code.component.html',
  styleUrl: './quantconnect-reference-code.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class QuantConnectReferenceCodeComponent {
  readonly referenceCode = input.required<StrategyReferenceCode>();
}
