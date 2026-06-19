import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import type {
  OptionContractMatch,
  SymbolMatch,
} from '../../../../../api/broker-models';
import { BrokerService } from '../../../../../services/broker.service';

/**
 * Drill-down option-leg picker (Slice 1F).
 *
 * Once the operator has selected an underlying via
 * ``<app-broker-instrument-card>``, this component walks the broker's
 * own metadata to pick a single contract:
 *
 *   expiry (broker.expirations)
 *     → strike (broker.strikes)
 *       → call / put toggle
 *         → ``broker.searchOptionContracts`` qualifies the (symbol,
 *           expiry, strike, right) tuple and emits the rich
 *           ``OptionContractMatch`` (con_id, local_symbol, multiplier).
 *
 * Same broker path that ``/broker/options-chain`` uses, so the leg the
 * operator declares is one IBKR will quote *and* fill.
 */
@Component({
  selector: 'app-option-leg-picker',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './option-leg-picker.component.html',
  styleUrl: './option-leg-picker.component.scss',
})
export class OptionLegPickerComponent {
  readonly symbol = input.required<SymbolMatch>();
  readonly qualify = output<OptionContractMatch>();

  private readonly broker = inject(BrokerService);

  readonly expirations = signal<readonly number[]>([]);
  readonly selectedExpiryMs = signal<number | null>(null);
  readonly strikes = signal<readonly number[]>([]);
  readonly selectedStrike = signal<number | null>(null);
  readonly selectedRight = signal<'C' | 'P'>('C');
  readonly loadingExpirations = signal<boolean>(false);
  readonly loadingStrikes = signal<boolean>(false);
  readonly qualifying = signal<boolean>(false);
  readonly error = signal<string | null>(null);

  readonly canQualify = computed<boolean>(
    () => this.selectedExpiryMs() !== null && this.selectedStrike() !== null,
  );

  constructor() {
    // Symbol changes (e.g. operator picks a different ticker on the
    // parent) reset the entire drill-down so the cockpit never shows
    // strikes/expiries from the previous underlying.
    effect(() => {
      this.symbol(); // track
      this.expirations.set([]);
      this.selectedExpiryMs.set(null);
      this.strikes.set([]);
      this.selectedStrike.set(null);
      this.selectedRight.set('C');
      this.error.set(null);
      void this._loadExpirations();
    });
  }

  selectExpiry(ms: number): void {
    this.selectedExpiryMs.set(ms);
    this.selectedStrike.set(null);
    this.strikes.set([]);
    void this._loadStrikes(ms);
  }

  selectStrike(strike: number): void {
    this.selectedStrike.set(strike);
  }

  selectRight(right: 'C' | 'P'): void {
    this.selectedRight.set(right);
  }

  async qualifyContract(): Promise<void> {
    const expiry = this.selectedExpiryMs();
    const strike = this.selectedStrike();
    if (expiry === null || strike === null) return;
    this.qualifying.set(true);
    this.error.set(null);
    try {
      const response = await this.broker.searchOptionContracts(
        this.symbol().symbol,
        expiry,
        strike,
        this.selectedRight(),
      );
      if (response.matches.length === 0) {
        this.error.set('IBKR could not qualify that contract.');
        return;
      }
      this.qualify.emit(response.matches[0]);
    } catch (err: unknown) {
      this.error.set(this._formatError(err, 'Failed to qualify contract.'));
    } finally {
      this.qualifying.set(false);
    }
  }

  trackExpiry = (_: number, ms: number): number => ms;
  trackStrike = (_: number, s: number): number => s;

  private async _loadExpirations(): Promise<void> {
    this.loadingExpirations.set(true);
    try {
      const response = await this.broker.expirations(this.symbol().symbol);
      this.expirations.set(response.expirations_ms);
    } catch (err: unknown) {
      this.error.set(this._formatError(err, 'Failed to load expirations.'));
    } finally {
      this.loadingExpirations.set(false);
    }
  }

  private async _loadStrikes(expiryMs: number): Promise<void> {
    this.loadingStrikes.set(true);
    try {
      const response = await this.broker.strikes(this.symbol().symbol, expiryMs);
      this.strikes.set(response.strikes);
    } catch (err: unknown) {
      this.error.set(this._formatError(err, 'Failed to load strikes.'));
    } finally {
      this.loadingStrikes.set(false);
    }
  }

  private _formatError(err: unknown, fallback: string): string {
    const status = (err as { status?: number })?.status ?? 0;
    if (status === 503) return 'IBKR offline — reconnect broker to qualify the leg.';
    if (status === 429) return 'Searching too fast — IBKR limits broker calls; please wait.';
    return fallback;
  }
}
