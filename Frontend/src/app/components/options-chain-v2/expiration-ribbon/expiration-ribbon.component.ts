import { Component, computed, input, output, ChangeDetectionStrategy } from '@angular/core';
import { optionExpiryDate } from '../../../shared/option-contract/option-expiry-date';

interface MonthGroup {
  label: string;
  year: number;
  /** Expiry anchor instants (16:00 New York time on each expiry date). */
  dates: number[];
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
  selectedDate = input<number | null>(null);
  loading = input(false);

  dateSelected = output<number>();

  private readonly MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  currentYear = new Date().getFullYear();

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
          dates: [],
        };
        groups.set(key, group);
      }
      group.dates.push(anchor);
    }

    return [...groups.values()];
  });

  selectDate(anchor: number): void {
    this.dateSelected.emit(anchor);
  }

  formatDay(anchor: number): string {
    return Number(optionExpiryDate(anchor).slice(8, 10)).toString();
  }

  isSelected(anchor: number): boolean {
    return anchor === this.selectedDate();
  }
}
