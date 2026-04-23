import {
  Component,
  signal,
  inject,
  ChangeDetectionStrategy,
} from "@angular/core";
import { CommonModule } from "@angular/common";
import { RouterModule } from "@angular/router";
import { HttpClient } from "@angular/common/http";
import { firstValueFrom } from "rxjs";
import {
  Accordion,
  AccordionContent,
  AccordionHeader,
  AccordionPanel,
} from "primeng/accordion";
import { KatexDirective } from "../../../shared/katex.directive";
import { environment } from "../../../../environments/environment";
import { PageHeaderComponent } from "../../../shared/page-header/page-header.component";

interface StepDoc {
  order: number;
  name: string;
  library: string;
  library_url: string | null;
  problem: string;
  fix: string;
  rules: string[];
  code: string;
  impact: string;
  formula_latex?: string;
}

interface LibraryInfo {
  name: string;
  purpose: string;
  url: string | null;
}

@Component({
  selector: "app-data-quality-docs",
  standalone: true,
  imports: [
    CommonModule,
    RouterModule,
    Accordion,
    AccordionContent,
    AccordionHeader,
    AccordionPanel,
    KatexDirective,
    PageHeaderComponent,
  ],
  templateUrl: "./data-quality-docs.component.html",
  styleUrl: "./data-quality-docs.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DataQualityDocsComponent {
  private http = inject(HttpClient);
  private baseUrl = environment.pythonServiceUrl;

  steps = signal<StepDoc[]>([]);
  loading = signal(true);
  error = signal("");

  libraries: LibraryInfo[] = [
    {
      name: "pandas_market_calendars",
      purpose: "NYSE trading schedule & early-close detection",
      url: "https://github.com/rsheftel/pandas_market_calendars",
    },
    {
      name: "pandas-ta",
      purpose: "Technical indicator computation (150+ indicators)",
      url: "https://github.com/twopirllc/pandas-ta",
    },
    {
      name: "pandas",
      purpose: "Data wrangling, VWAP computation, filtering",
      url: null,
    },
    {
      name: "zoneinfo (stdlib)",
      purpose: "DST-aware timezone conversion (UTC → America/New_York)",
      url: null,
    },
    {
      name: "numpy",
      purpose: "Numeric operations, NaN handling",
      url: null,
    },
  ];

  pipelineOrder = [
    "Fetch raw aggregates (UTC timestamps)",
    "Convert to NY timezone",
    "Filter to NYSE valid minutes (pandas_market_calendars)",
    "Drop fabricated bars (volume=0 stale)",
    "Fix fractional volume (drop or round)",
    "Recompute VWAP (daily reset)",
    "Recompute indicators with warmup window",
    "Slice export window only at the end",
  ];

  constructor() {
    this.loadDocs();
  }

  async loadDocs(): Promise<void> {
    try {
      const res = await firstValueFrom(
        this.http.get<{ success: boolean; steps: StepDoc[] }>(
          `${this.baseUrl}/api/data-quality/docs`
        )
      );
      this.steps.set(res.steps);
    } catch (e: any) {
      this.error.set(e?.message || "Failed to load documentation");
    } finally {
      this.loading.set(false);
    }
  }
}
