import { ChangeDetectionStrategy, Component, input, signal } from '@angular/core';

import type { StrategyReferenceCode } from '../../../services/strategy-validation.types';

@Component({
  selector: 'app-quantconnect-reference-code',
  templateUrl: './quantconnect-reference-code.component.html',
  styleUrl: './quantconnect-reference-code.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class QuantConnectReferenceCodeComponent {
  readonly referenceCode = input.required<StrategyReferenceCode>();

  private readonly copiedSha = signal<string | null>(null);
  protected readonly copyError = signal(false);

  protected isCopied(code: StrategyReferenceCode): boolean {
    return this.copiedSha() === code.sha256;
  }

  protected async copyAlgorithm(): Promise<void> {
    const code = this.referenceCode();
    this.copyError.set(false);

    try {
      if (typeof navigator === 'undefined' || !navigator.clipboard) {
        throw new Error('Clipboard API unavailable');
      }
      await navigator.clipboard.writeText(code.source);
      this.copiedSha.set(code.sha256);
    } catch {
      this.copiedSha.set(null);
      this.copyError.set(true);
    }
  }
}
