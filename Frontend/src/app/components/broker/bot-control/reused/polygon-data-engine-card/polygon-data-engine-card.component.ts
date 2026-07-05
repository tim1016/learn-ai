import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { DataPlaneHealth } from '../../../../../api/broker-models';
import { fmtTimestampNy } from '../../../format';

interface EngineMetric {
  readonly label: string;
  readonly value: string;
  readonly mono: boolean;
}

@Component({
  selector: 'app-polygon-data-engine-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './polygon-data-engine-card.component.html',
  styleUrl: './polygon-data-engine-card.component.scss',
})
export class PolygonDataEngineCardComponent {
  readonly health = input<DataPlaneHealth | null>(null);
  readonly loading = input(false);

  readonly databaseLayerTransforms = ['translate(0, 160)', 'translate(0, 70)'] as const;
  readonly mainGearToothTransforms = [
    'rotate(0)',
    'rotate(45)',
    'rotate(90)',
    'rotate(135)',
    'rotate(180)',
    'rotate(225)',
    'rotate(270)',
    'rotate(315)',
  ] as const;
  readonly smallGearToothTransforms = [
    'rotate(22.5)',
    'rotate(67.5)',
    'rotate(112.5)',
    'rotate(157.5)',
    'rotate(202.5)',
    'rotate(247.5)',
    'rotate(292.5)',
    'rotate(337.5)',
  ] as const;

  readonly serviceName = computed(() => this.health()?.service ?? 'polygon-data-service');
  readonly metrics = computed<readonly EngineMetric[] | null>(() => {
    const health = this.health();
    if (health === null) return null;
    return [
      { label: 'revision', value: health.code_revision.slice(0, 12), mono: true },
      { label: 'started', value: fmtTimestampNy(health.process_start_ms), mono: false },
      { label: 'reload', value: health.reload, mono: true },
    ];
  });
}
