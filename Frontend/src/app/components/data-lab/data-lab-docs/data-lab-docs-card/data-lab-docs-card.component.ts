import { ChangeDetectionStrategy, Component, computed, input, model, output, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { TabsModule } from 'primeng/tabs';
import { KatexDirective } from '../../../../shared/katex.directive';

export type IndicatorTab = 'explain' | 'math' | 'use';

export interface IndicatorDoc {
  name: string;
  displayName: string;
  formulaLatex: string;
  description: string;
  library: string;
  outputColumns: string[];
  defaultParams: string;
  interpretation: string[];
  recommendedTimeframes: string;
  dataNotes: string[];
  relatedIndicators: string[];
  panelType: 'overlay' | 'sub-panel';
  quickWhy: string;
  quickAnalogy: string;
  quickImpact: string;
  checkQuestion?: string;
  checkAnswer?: string;
  professionalRef: string;
  timeframeBehavior: string;
}

export type CaveatSeverity = 'info' | 'warn' | 'risk';

export interface CaveatRow {
  severity: CaveatSeverity;
  text: string;
}

export function severityForNote(note: string): CaveatSeverity {
  const s = note.toLowerCase();
  if (/15[- ]?minute delay|real[- ]?time|scalp|live|extended.hours/.test(s)) return 'risk';
  if (/warmup|warm-up|missing|nan|gap|session|drift|volume-dependent/.test(s)) return 'warn';
  return 'info';
}

const SEVERITY_LABEL: Record<CaveatSeverity, string> = { info: 'Info', warn: 'Warn', risk: 'Risk' };

@Component({
  selector: 'app-data-lab-docs-card',
  standalone: true,
  imports: [CommonModule, TabsModule, KatexDirective],
  templateUrl: './data-lab-docs-card.component.html',
  styleUrls: ['./data-lab-docs-card.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DataLabDocsCardComponent {
  indicator = input.required<IndicatorDoc>();
  activeTab = model<IndicatorTab>('explain');

  relatedClick = output<string>();

  protected revealed = signal(false);

  protected tickerCode = computed(() => this.indicator().name.toUpperCase());

  protected panelLabel = computed(() =>
    this.indicator().panelType === 'overlay' ? 'Overlay' : 'Sub-panel'
  );

  protected breadcrumb = computed(() => {
    const ind = this.indicator();
    const kind = ind.panelType === 'overlay' ? 'Overlay' : 'Sub-panel';
    return `Docs \u203A ${kind} \u203A ${ind.displayName}`;
  });

  protected colCountLabel = computed(() => {
    const n = this.indicator().outputColumns.length;
    return n === 1 ? '1 column' : `${n} columns`;
  });

  protected hasDelay = computed(() => {
    const s = `${this.indicator().timeframeBehavior} ${this.indicator().dataNotes.join(' ')}`.toLowerCase();
    return /15[- ]?minute delay|real[- ]?time|scalp/.test(s);
  });

  protected hasWarmup = computed(() =>
    this.indicator().dataNotes.some(n => /warmup|warm-up/.test(n.toLowerCase()))
  );

  protected caveats = computed<CaveatRow[]>(() => {
    const ind = this.indicator();
    const rows: CaveatRow[] = ind.dataNotes.map(text => ({ severity: severityForNote(text), text }));
    const order: CaveatSeverity[] = ['risk', 'warn', 'info'];
    if (this.hasDelay() && !rows.some(r => r.severity === 'risk')) {
      rows.unshift({ severity: 'risk', text: `15-minute feed delay: ${ind.timeframeBehavior}` });
    }
    return rows.sort((a, b) => order.indexOf(a.severity) - order.indexOf(b.severity));
  });

  protected signalTone(text: string): 'bull' | 'bear' | 'warn' | 'neutral' {
    const s = text.toLowerCase();
    if (/bullish|above|buy|upward|positive momentum|accumulation|strong uptrend|cross.*above|rising/.test(s)) return 'bull';
    if (/bearish|below|sell|downward|negative momentum|distribution|strong downtrend|cross.*below|falling/.test(s)) return 'bear';
    if (/overbought|oversold|divergence|warn|reversal|caution|weaken/.test(s)) return 'warn';
    return 'neutral';
  }

  protected signalLabel(text: string): string {
    const first = text.split(/[\u2014\u2013\-:\u2192]/)[0]?.trim() ?? '';
    if (first.length > 0 && first.length <= 32) return first;
    return text.split(' ').slice(0, 3).join(' ');
  }

  protected signalBody(text: string): string {
    const parts = text.split(/[\u2014\u2013\-:\u2192]/);
    if (parts.length > 1) return parts.slice(1).join(' \u2014 ').trim();
    return text;
  }

  protected severityLabel(sev: CaveatSeverity): string {
    return SEVERITY_LABEL[sev];
  }

  protected onTabChange(value: unknown): void {
    if (value === 'explain' || value === 'math' || value === 'use') {
      this.activeTab.set(value);
    }
  }

  protected toggleReveal(): void {
    this.revealed.update(v => !v);
  }

  protected onRelatedClick(name: string, event: MouseEvent): void {
    event.preventDefault();
    this.relatedClick.emit(name);
  }
}
