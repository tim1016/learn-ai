import { Component, ChangeDetectionStrategy, signal, inject, DestroyRef, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Tab, TabList, TabPanel, TabPanels, Tabs } from 'primeng/tabs';
import { PortfolioService } from '../../services/portfolio.service';
import { Account } from '../../graphql/portfolio-types';
import { DashboardComponent } from './dashboard/dashboard.component';
import { PositionsComponent } from './positions/positions.component';
import { EquityChartComponent } from './equity-chart/equity-chart.component';
import { RiskPanelComponent } from './risk-panel/risk-panel.component';
import { ScenarioExplorerComponent } from './scenario-explorer/scenario-explorer.component';
import { ReconciliationComponent } from './reconciliation/reconciliation.component';
import { StrategyAttributionComponent } from './strategy-attribution/strategy-attribution.component';
import { catchError, finalize, of } from 'rxjs';

@Component({
  selector: 'app-portfolio',
  standalone: true,
  imports: [
    CommonModule, FormsModule,
    Tabs, TabList, Tab, TabPanel, TabPanels,
    DashboardComponent, PositionsComponent, EquityChartComponent,
    RiskPanelComponent, ScenarioExplorerComponent,
    ReconciliationComponent, StrategyAttributionComponent,
  ],
  templateUrl: './portfolio.component.html',
  styleUrls: ['./portfolio.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PortfolioComponent {
  private portfolioService = inject(PortfolioService);
  private destroyRef = inject(DestroyRef);

  accounts = signal<Account[]>([]);
  selectedAccountId = signal<string | null>(null);
  loadingAccounts = signal(false);
  error = signal<string | null>(null);

  // New account form
  showCreateForm = signal(false);
  newAccountName = signal('');
  newAccountType = signal('Paper');
  newAccountCash = signal(100_000);
  creating = signal(false);

  selectedAccount = computed(() => {
    const id = this.selectedAccountId();
    return this.accounts().find(a => a.id === id) ?? null;
  });

  accountTypes = ['Paper', 'Live', 'Backtest'];

  ngOnInit(): void {
    this.loadAccounts();
  }

  loadAccounts(): void {
    this.loadingAccounts.set(true);
    this.portfolioService.getAccounts().pipe(
      takeUntilDestroyed(this.destroyRef),
      catchError(err => { this.error.set(err?.message); return of([]); }),
      finalize(() => this.loadingAccounts.set(false)),
    ).subscribe(accounts => {
      this.accounts.set(accounts);
      if (accounts.length > 0 && !this.selectedAccountId()) {
        this.selectedAccountId.set(accounts[0].id);
      }
    });
  }

  createAccount(): void {
    const name = this.newAccountName().trim();
    if (!name) return;
    this.creating.set(true);
    this.portfolioService.createAccount(name, this.newAccountType(), this.newAccountCash()).pipe(
      takeUntilDestroyed(this.destroyRef),
      catchError(err => { this.error.set(err?.message); return of(null); }),
      finalize(() => this.creating.set(false)),
    ).subscribe(result => {
      if (result?.success && result.account) {
        this.accounts.update(list => [...list, result.account!]);
        this.selectedAccountId.set(result.account.id);
        this.showCreateForm.set(false);
        this.newAccountName.set('');
      } else if (result?.error) {
        this.error.set(result.error);
      }
    });
  }
}
