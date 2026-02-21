import { Component, computed, input, output, ChangeDetectionStrategy } from '@angular/core';

interface MonthGroup {
  label: string;
  year: number;
  dates: string[];
}

@Component({
  selector: 'app-expiration-ribbon',
  standalone: true,
  templateUrl: './expiration-ribbon.component.html',
  styleUrls: ['./expiration-ribbon.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ExpirationRibbonComponent {
  expirations = input.required<string[]>();
  selectedDate = input<string | null>(null);
  loading = input(false);

  dateSelected = output<string>();

  private readonly MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  currentYear = new Date().getFullYear();

  monthGroups = computed<MonthGroup[]>(() => {
    const exps = this.expirations();
    const groups = new Map<string, MonthGroup>();

    for (const dateStr of exps) {
      const d = new Date(dateStr + 'T00:00:00');
      const month = d.getMonth();
      const year = d.getFullYear();
      const key = `${year}-${month}`;

      if (!groups.has(key)) {
        groups.set(key, {
          label: this.MONTHS[month],
          year,
          dates: [],
        });
      }
      groups.get(key)!.dates.push(dateStr);
    }

    return [...groups.values()];
  });

  selectDate(date: string): void {
    this.dateSelected.emit(date);
  }

  formatDay(dateStr: string): string {
    return new Date(dateStr + 'T00:00:00').getDate().toString();
  }

  isSelected(dateStr: string): boolean {
    return dateStr === this.selectedDate();
  }
}
