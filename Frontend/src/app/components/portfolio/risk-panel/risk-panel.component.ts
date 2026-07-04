import { Component, ChangeDetectionStrategy, input, signal, inject, DestroyRef, effect } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, finalize, of } from 'rxjs';
import { PortfolioService } from '../../../services/portfolio.service';
import { RiskRule, RiskViolation, DollarDeltaResult } from '../../../graphql/portfolio-types';

@Component({
  selector: 'app-risk-panel',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './risk-panel.component.html',
  styleUrls: ['./risk-panel.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class RiskPanelComponent {
  accountId = input.required<string>();
  private portfolioService = inject(PortfolioService);
  private destroyRef = inject(DestroyRef);

  rules = signal<RiskRule[]>([]);
  violations = signal<RiskViolation[]>([]);
  deltaResults = signal<DollarDeltaResult[]>([]);
  portfolioVega = signal<number | null>(null);
  loading = signal(false);
  error = signal<string | null>(null);

  // New rule form
  newRuleType = signal('MaxDrawdown');
  newThreshold = signal(0.1);
  newAction = signal('Warn');
  newSeverity = signal('Medium');
  creating = signal(false);

  ruleTypes = ['MaxDrawdown', 'MaxPositionSize', 'MaxVegaExposure', 'MaxDelta'];
  actions = ['Warn', 'Block'];
  severities = ['Low', 'Medium', 'High', 'Critical'];

  constructor() {
    effect(() => { if (this.accountId()) this.loadRules(); });
  }

  loadRules(): void {
    this.loading.set(true);
    this.portfolioService.getRiskRules(this.accountId()).pipe(
      takeUntilDestroyed(this.destroyRef),
      catchError(err => { this.error.set(err?.message); return of([]); }),
      finalize(() => this.loading.set(false)),
    ).subscribe(r => this.rules.set(r));
  }

  createRule(): void {
    this.creating.set(true);
    this.portfolioService.createRiskRule(
      this.accountId(), this.newRuleType(), this.newThreshold(),
      this.newAction(), this.newSeverity(),
    ).pipe(
      takeUntilDestroyed(this.destroyRef),
      finalize(() => this.creating.set(false)),
    ).subscribe(res => {
      if (res.success && res.rule) {
        this.rules.update(list => [...list, res.rule!]);
      } else {
        this.error.set(res.error ?? 'Failed to create rule');
      }
    });
  }

  toggleRule(rule: RiskRule): void {
    this.portfolioService.updateRiskRule(rule.id, { enabled: !rule.enabled }).pipe(
      takeUntilDestroyed(this.destroyRef),
    ).subscribe(res => {
      if (res.success) this.loadRules();
    });
  }

  evaluateRules(): void {
    this.loading.set(true);
    this.portfolioService.evaluateRiskRules(this.accountId(), []).pipe(
      takeUntilDestroyed(this.destroyRef),
      catchError(err => { this.error.set(err?.message); return of([]); }),
      finalize(() => this.loading.set(false)),
    ).subscribe(v => this.violations.set(v));
  }

  loadVega(): void {
    this.portfolioService.getDollarDelta(this.accountId(), []).pipe(
      takeUntilDestroyed(this.destroyRef),
    ).subscribe(d => this.deltaResults.set(d));
  }
}
