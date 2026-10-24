import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../../../environments/environment';
import { DataLabWorkspaceStore } from '../data-lab-workspace-store';

interface ValidationResponse {
  success: boolean;
  report: string;
}

/**
 * Data Lab Validate — one readable column: pandas-ta CSV vs
 * TradingView CSV comparison and report display/download. Quality evidence
 * lives on the instrument's Data tab. The past-chain inspector stays under the
 * Build dataset options companion — linked from here.
 */
@Component({
  selector: 'app-data-lab-validate',
  imports: [RouterLink],
  templateUrl: './validate.component.html',
  styleUrls: ['./validate.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ValidateComponent {
  private readonly http = inject(HttpClient);
  readonly store = inject(DataLabWorkspaceStore);

  readonly ourCsvFile = signal<File | null>(null);
  readonly tvCsvFile = signal<File | null>(null);
  readonly loadingValidation = signal(false);
  readonly validationError = signal('');
  readonly validationReport = signal('');

  /** Ticker the validation requests are labeled with — defaults to the
   *  committed workspace ticker; editable for one-off comparisons. */
  readonly ticker = computed(() => this.store.committedTicker() || 'SPY');

  readonly canRunValidation = computed(
    () => !!this.ourCsvFile() && !!this.tvCsvFile() && !this.loadingValidation(),
  );

  onOurCsvSelected(event: Event): void {
    const input = event.target as HTMLInputElement;
    this.ourCsvFile.set(input.files?.[0] ?? null);
  }

  onTvCsvSelected(event: Event): void {
    const input = event.target as HTMLInputElement;
    this.tvCsvFile.set(input.files?.[0] ?? null);
  }

  async runValidation(): Promise<void> {
    const ourFile = this.ourCsvFile();
    const tvFile = this.tvCsvFile();
    if (!ourFile || !tvFile) return;

    this.loadingValidation.set(true);
    this.validationError.set('');
    this.validationReport.set('');

    try {
      const formData = new FormData();
      formData.append('our_csv', ourFile);
      formData.append('tv_csv', tvFile);
      formData.append('ticker', this.ticker());

      const response = await firstValueFrom(
        this.http.post<ValidationResponse>(
          `${environment.pythonServiceUrl}/api/dataset/validation-report`,
          formData,
        ),
      );

      if (response.success) {
        this.validationReport.set(response.report);
      } else {
        this.validationError.set('Validation report generation failed');
      }
    } catch (e: unknown) {
      this.validationError.set(e instanceof Error ? e.message : String(e));
    } finally {
      this.loadingValidation.set(false);
    }
  }

  downloadValidationReport(): void {
    const report = this.validationReport();
    if (!report) return;
    const blob = new Blob([report], { type: 'text/markdown' });
    this.downloadBlob(blob, `${this.ticker()}_validation_report.md`);
  }
  private downloadBlob(blob: Blob, filename: string): void {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }
}
