import { Component, ChangeDetectionStrategy, signal, inject, DestroyRef, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, finalize, of } from 'rxjs';
import { PortfolioService } from '../../../services/portfolio.service';
import { ValidationSuiteResult, ValidationTestResult } from '../../../graphql/portfolio-types';

@Component({
  selector: 'app-validation',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './validation.component.html',
  styleUrls: ['./validation.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ValidationComponent {
  private portfolioService = inject(PortfolioService);
  private destroyRef = inject(DestroyRef);

  result = signal<ValidationSuiteResult | null>(null);
  running = signal(false);
  error = signal<string | null>(null);
  expandedTests = signal<Set<number>>(new Set());

  passRate = computed(() => {
    const r = this.result();
    if (!r || r.totalTests === 0) return 0;
    return Math.round((r.passed / r.totalTests) * 100);
  });

  categories = computed(() => {
    const r = this.result();
    if (!r) return [];
    const map = new Map<string, { passed: number; failed: number }>();
    for (const t of r.tests) {
      const cat = map.get(t.category) ?? { passed: 0, failed: 0 };
      if (t.passed) cat.passed++;
      else cat.failed++;
      map.set(t.category, cat);
    }
    return Array.from(map.entries()).map(([name, counts]) => ({ name, ...counts }));
  });

  runValidation(): void {
    this.running.set(true);
    this.error.set(null);
    this.result.set(null);
    this.expandedTests.set(new Set());

    this.portfolioService.runValidation().pipe(
      takeUntilDestroyed(this.destroyRef),
      catchError(err => { this.error.set(err?.message ?? 'Validation failed'); return of(null); }),
      finalize(() => this.running.set(false)),
    ).subscribe(r => {
      if (r) this.result.set(r);
    });
  }

  toggleTest(testNumber: number): void {
    this.expandedTests.update(set => {
      const next = new Set(set);
      if (next.has(testNumber)) next.delete(testNumber);
      else next.add(testNumber);
      return next;
    });
  }

  isExpanded(testNumber: number): boolean {
    return this.expandedTests().has(testNumber);
  }

  expandAll(): void {
    const r = this.result();
    if (!r) return;
    this.expandedTests.set(new Set(r.tests.map(t => t.testNumber)));
  }

  collapseAll(): void {
    this.expandedTests.set(new Set());
  }

  expandFailed(): void {
    const r = this.result();
    if (!r) return;
    this.expandedTests.set(new Set(r.tests.filter(t => !t.passed).map(t => t.testNumber)));
  }

  get failedAssertionCount(): number {
    const r = this.result();
    if (!r) return 0;
    return r.tests.reduce((sum, t) => sum + t.assertions.filter(a => !a.passed).length, 0);
  }
}
