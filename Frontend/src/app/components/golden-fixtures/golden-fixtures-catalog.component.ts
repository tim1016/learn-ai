import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  resource,
} from '@angular/core';
import { SlicePipe } from '@angular/common';
import { GoldenFixturesService } from '../../services/golden-fixtures.service';
import type { FixtureSummary } from '../../services/golden-fixtures.types';
import {
  CATEGORY_LABELS,
  REFERENCE_KIND_LABELS,
} from '../../services/golden-fixtures.types';

type BadgeLevel = 'certified' | 'vendor' | 'regression' | 'pending' | 'breach';

interface FixtureRow {
  fixture: FixtureSummary;
  badge: BadgeLevel;
  badgeLabel: string;
}

interface CategoryGroup {
  key: string;
  label: string;
  rows: FixtureRow[];
}

const CERTIFIED_KINDS = new Set([
  'cross_engine',
  'external_reference',
  'literature_formula',
  'hand_computed',
]);

function badgeFor(f: FixtureSummary): BadgeLevel {
  if (f.status === 'breach') return 'breach';
  if (f.status === 'planned' || f.status === 'generated') return 'pending';
  if (f.reference_kind === 'vendor_observed') return 'vendor';
  if (f.reference_kind === 'internal_regression') return 'regression';
  if (CERTIFIED_KINDS.has(f.reference_kind)) return 'certified';
  return 'pending';
}

@Component({
  selector: 'app-golden-fixtures-catalog',
  templateUrl: './golden-fixtures-catalog.component.html',
  styleUrl: './golden-fixtures-catalog.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [SlicePipe],
})
export class GoldenFixturesCatalogComponent {
  private readonly svc = inject(GoldenFixturesService);

  protected readonly catalog = resource({
    loader: () => this.svc.getCatalog(),
  });

  protected readonly validation = computed(() => this.catalog.value()?.validation ?? null);

  protected readonly totalFixtures = computed(
    () => this.catalog.value()?.fixtures.length ?? 0,
  );

  protected readonly certifiedCount = computed(
    () =>
      this.catalog.value()?.fixtures.filter((f) => f.is_certified).length ?? 0,
  );

  protected readonly categoryGroups = computed<CategoryGroup[]>(() => {
    const fixtures = this.catalog.value()?.fixtures ?? [];
    const grouped = new Map<string, FixtureSummary[]>();
    for (const f of fixtures) {
      const list = grouped.get(f.category) ?? [];
      list.push(f);
      grouped.set(f.category, list);
    }
    return [...grouped.entries()].map(([key, items]) => ({
      key,
      label: CATEGORY_LABELS[key] ?? key,
      rows: items.map((f) => ({
        fixture: f,
        badge: badgeFor(f),
        badgeLabel: REFERENCE_KIND_LABELS[f.reference_kind] ?? f.reference_kind,
      })),
    }));
  });

  protected formatAtol(v: number): string {
    return v.toExponential(0);
  }

  protected validationStatusClass(): string {
    const v = this.validation();
    if (!v) return '';
    if (v.status === 'ok') return 'status-ok';
    if (v.failed > 0 || v.errors > 0) return 'status-fail';
    return 'status-unknown';
  }
}
