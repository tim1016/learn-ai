import { afterNextRender, ChangeDetectionStrategy, Component, computed, ElementRef, inject, Injector, input, resource, viewChild } from '@angular/core';

import { AssetIdentityComponent } from '../../../../shared/asset-identity';
import { TimestampDisplayComponent } from '../../../../shared/timestamp';
import { extractServerMessage } from '../../../broker/operation-error';
import { RunDetailRefusedError, RunDetailService } from '../run-detail.service';
import type { ComparedRun } from '../run-detail.types';
import { CompareSessionsComponent } from './compare-sessions.component';
import { CompareSettingsComponent } from './compare-settings.component';

interface Side {
  readonly role: 'A' | 'B';
  readonly run: ComparedRun;
}

/**
 * Two complete runs of one underlying side by side, worked out by the server. First what differs between them, setting by
 * setting and then the dates, so that unlike runs are never compared without knowing it; only then the sessions they share
 * and each run's figures over them. The page shows the server's words and figures and subtracts nothing. A refusal (a run
 * that is not complete, runs of different underlyings, a run whose setup can no longer be read, a run that is gone) is
 * the server's own words. The read is for these two runs; an answer for two others is dropped.
 */
@Component({
  selector: 'app-options-backtest-compare-view',
  imports: [AssetIdentityComponent, TimestampDisplayComponent, CompareSettingsComponent, CompareSessionsComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './compare-view.component.html',
  styleUrl: './compare-view.component.scss',
})
export class CompareViewComponent {
  private readonly service = inject(RunDetailService);
  private readonly injector = inject(Injector);

  readonly a = input.required<number>();
  readonly b = input.required<number>();

  private readonly read = resource({
    params: () => ({ a: this.a(), b: this.b() }),
    loader: ({ params }) => this.service.compare(params.a, params.b),
  });
  protected readonly comparison = computed(() => (this.read.hasValue() ? this.read.value() : null));
  protected readonly reading = computed(() => this.comparison() === null && this.read.error() === undefined);
  protected readonly problem = computed(() => {
    const error: unknown = this.read.error();
    if (error === undefined) return null;
    return error instanceof RunDetailRefusedError ? error.message : extractServerMessage(error, 'The comparison could not be read.');
  });
  protected readonly sides = computed<readonly Side[]>(() => {
    const comparison = this.comparison();
    return comparison === null ? [] : [{ role: 'A', run: comparison.a }, { role: 'B', run: comparison.b }];
  });

  private readonly heading = viewChild.required<ElementRef<HTMLElement>>('heading');

  /** Puts the keyboard on the view's heading, where the owner has just asked to be. */
  focusHeading(): void {
    afterNextRender(() => this.heading().nativeElement.focus(), { injector: this.injector });
  }

  protected retry(): void {
    this.read.reload();
  }
}
