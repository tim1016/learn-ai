import { Component, computed, input, output, ChangeDetectionStrategy } from '@angular/core';
import { etIsoDate } from '../../../shared/date/et-midnight';
import { optionExpiryDate } from '../../../shared/option-contract/option-expiry-date';

interface MonthGroup {
  label: string;
  year: number;
  /** Expiry anchor instants (16:00 New York time on each expiry date). */
  expirations: number[];
}

@Component({
  selector: 'app-expiration-ribbon',
  standalone: true,
  templateUrl: './expiration-ribbon.component.html',
  styleUrls: ['./expiration-ribbon.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ExpirationRibbonComponent {
  expirations = input.required<number[]>();
  selectedExpiration = input<number | null>(null);
  loading = input(false);

  expirationSelected = output<number>();

  private readonly MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  // The year of today's New York date, the zone `group.year` is read in.
  currentYear = Number(etIsoDate(Date.now()).slice(0, 4));

  monthGroups = computed<MonthGroup[]>(() => {
    const exps = this.expirations();
    const groups = new Map<string, MonthGroup>();

    for (const anchor of exps) {
      // The anchor names a New York date; group by that calendar date, read in
      // New York, never in the browser's zone (#2934).
      const [year, month] = optionExpiryDate(anchor).split('-').map(Number);
      const key = `${year}-${month}`;

      let group = groups.get(key);
      if (!group) {
        group = {
          label: this.MONTHS[month - 1],
          year,
          expirations: [],
        };
        groups.set(key, group);
      }
      group.expirations.push(anchor);
    }

    return [...groups.values()];
  });

  selectExpiration(anchor: number): void {
    this.expirationSelected.emit(anchor);
  }

  formatDay(anchor: number): string {
    return Number(optionExpiryDate(anchor).slice(8, 10)).toString();
  }

  isSelected(anchor: number): boolean {
    return anchor === this.selectedExpiration();
  }
}
