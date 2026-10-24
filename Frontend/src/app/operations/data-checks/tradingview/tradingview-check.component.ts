import { ChangeDetectionStrategy, Component, computed, inject, input, linkedSignal, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../../../environments/environment';
import { instrumentCommands } from '../../../markets/instrument-address';
import { SymbolPickerComponent } from '../../../shared/symbol-picker/symbol-picker.component';

interface ValidationResponse {
  success: boolean;
  report: string;
}

/**
 * Operations → Data checks → TradingView comparison — one readable column:
 * pandas-ta CSV vs TradingView CSV comparison, with the report to read and
 * download. The past-chain inspector stays under the Build dataset options
 * companion — linked from here.
 *
 * The comparison reads only the two uploaded files, so it has a symbol (it
 * labels the request and the report) and no dates. The quality evidence that
 * once sat here belongs to the instrument's Data tab. A link that carried a
 * `ticker` starts the picker there; the uploaded files cannot travel in a
 * link. Opening the page uploads and runs nothing.
 */
@Component({
  selector: 'app-tradingview-check',
  imports: [RouterLink, SymbolPickerComponent],
  templateUrl: './tradingview-check.component.html',
  styleUrls: ['./tradingview-check.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class TradingviewCheckComponent {
  private readonly http = inject(HttpClient);

  /** The symbol a link carried, bound from its query. */
  readonly ticker = input<string>();

  /** The picker's value. A link carrying a new symbol replaces it; an edit holds until then. */
  protected readonly symbol = linkedSignal(() => (this.ticker() ?? '').trim().toUpperCase() || 'SPY');
  /** Where Build dataset is for the instrument the picker holds. */
  protected readonly buildDataset = computed(() => instrumentCommands(this.symbol() || 'SPY', 'data', ['build']));

  readonly ourCsvFile = signal<File | null>(null);
  readonly tvCsvFile = signal<File | null>(null);
  readonly loadingValidation = signal(false);
  readonly validationError = signal('');
  readonly validationReport = signal('');

  /** The ticker the requests and the downloaded reports are labeled with. */
  private readonly label = computed(() => this.symbol() || 'SPY');

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
      formData.append('ticker', this.label());

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
    this.downloadBlob(blob, `${this.label()}_validation_report.md`);
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
