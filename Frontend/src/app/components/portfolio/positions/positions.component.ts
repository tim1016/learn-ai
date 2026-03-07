import { Component, ChangeDetectionStrategy, input, signal, inject, DestroyRef, effect } from '@angular/core';
import { CommonModule } from '@angular/common';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, finalize, of } from 'rxjs';
import { PortfolioService } from '../../../services/portfolio.service';
import { Position } from '../../../graphql/portfolio-types';

@Component({
  selector: 'app-positions',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './positions.component.html',
  styleUrls: ['./positions.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class PositionsComponent {
  accountId = input.required<string>();
  private portfolioService = inject(PortfolioService);
  private destroyRef = inject(DestroyRef);

  positions = signal<Position[]>([]);
  loading = signal(false);
  error = signal<string | null>(null);
  expandedId = signal<string | null>(null);
  showClosed = signal(false);

  get filteredPositions(): Position[] {
    const all = this.positions();
    return this.showClosed() ? all : all.filter(p => p.status === 'Open');
  }

  constructor() {
    effect(() => { if (this.accountId()) this.load(); });
  }

  load(): void {
    this.loading.set(true);
    this.portfolioService.getPositions(this.accountId()).pipe(
      takeUntilDestroyed(this.destroyRef),
      catchError(err => { this.error.set(err?.message); return of([]); }),
      finalize(() => this.loading.set(false)),
    ).subscribe(p => this.positions.set(p));
  }

  toggleExpand(id: string): void {
    this.expandedId.set(this.expandedId() === id ? null : id);
  }

  rebuild(): void {
    this.loading.set(true);
    this.portfolioService.rebuildPositions(this.accountId()).pipe(
      takeUntilDestroyed(this.destroyRef),
      finalize(() => this.loading.set(false)),
    ).subscribe(() => this.load());
  }
}
