import { NgTemplateOutlet } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed } from '@angular/core';
import { RouterLink } from '@angular/router';

import { operationsTabRoute } from '../../operations/operations-tabs';
import { formatTimestampDisplay } from '../timestamp/timestamp-display';
import { HEALTH_SOURCES, SEVERITY_WORD, SOURCE_LABEL, type HealthSeverity } from './scoped-health';
import { watchScopedHealth } from './scoped-health.service';

interface Chip {
  readonly source: string;
  readonly label: string;
  readonly severity: HealthSeverity;
  readonly word: string;
  /** Findings that ask for attention, beyond the one the chip shows. */
  readonly attention: number;
  readonly detail: string;
  readonly link: { readonly route: readonly string[]; readonly query: Readonly<Record<string, string>>; readonly fragment: string | undefined } | null;
}

/**
 * The health chips of the Operations header: one per source, each with the
 * worst finding the projection holds for it (PRD #3490 R30).
 *
 * A source the projection does not mention, or a projection that cannot be
 * read, is a chip that reads unknown. No path in here produces OK from
 * silence.
 */
@Component({
  selector: 'app-health-chips',
  imports: [NgTemplateOutlet, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './health-chips.component.html',
  styleUrl: './health-chips.component.scss',
})
export class HealthChipsComponent {
  private readonly projection = watchScopedHealth();

  protected readonly chips = computed<readonly Chip[]>(() => {
    if (this.projection.error() !== undefined) return [unknownChip('Health', 'Health status could not be read.')];
    if (!this.projection.hasValue()) return [];
    const items = this.projection.value().items;
    return HEALTH_SOURCES.map((source) => {
      const own = items.filter((item) => item.source === source);
      // The data plane sends each source's items worst first.
      const [worst] = own;
      if (worst === undefined) return unknownChip(SOURCE_LABEL[source], 'The data plane did not report this source.');
      const attention = own.filter((item) => item.needs_attention).length;
      return {
        source,
        label: SOURCE_LABEL[source],
        severity: worst.severity,
        word: SEVERITY_WORD[worst.severity],
        attention: Math.max(attention - 1, 0),
        detail: `${worst.summary} (read ${formatTimestampDisplay(worst.observed_ms, { mode: 'local' })}${worst.freshness === 'cached' ? ', reused' : ''})`,
        link: worst.link === null || worst.link === undefined ? null : {
          route: operationsTabRoute(worst.link.tab),
          query: worst.link.query ?? {},
          fragment: worst.link.fragment ?? undefined,
        },
      };
    });
  });
}

function unknownChip(label: string, detail: string): Chip {
  return { source: label, label, severity: 'unknown', word: SEVERITY_WORD.unknown, attention: 0, detail, link: null };
}
