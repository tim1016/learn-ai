import { ChangeDetectionStrategy, Component, effect, inject, linkedSignal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router } from '@angular/router';
import { TagModule } from 'primeng/tag';

import { AlpacaDeployDrawerComponent } from '../../broker/broker-deploy-page/alpaca-deploy-drawer.component';
import { AlpacaAccountCardComponent } from './alpaca-account-card.component';
import { AlpacaOperatorLensComponent } from './alpaca-operator-lens.component';
import { AlpacaOperatorLensDataService } from './alpaca-operator-lens-data.service';
import { AlpacaTraderLensComponent } from './alpaca-trader-lens.component';

const LENS_STORAGE_KEY = 'learn-ai.alpaca-desk.lens';

type AlpacaDeskLens = 'trader' | 'operator';

function lensFrom(value: string | null): AlpacaDeskLens | null {
  return value === 'trader' || value === 'operator' ? value : null;
}

function storedLens(): AlpacaDeskLens | null {
  if (typeof localStorage === 'undefined') return null;
  try {
    return lensFrom(localStorage.getItem(LENS_STORAGE_KEY));
  } catch (error) {
    // Storage can be disabled without making the in-memory desk unusable.
    void error;
    return null;
  }
}

function persistLens(lens: AlpacaDeskLens): void {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(LENS_STORAGE_KEY, lens);
  } catch (error) {
    // Persistence is an enhancement; keep the current session's choice.
    void error;
  }
}

/**
 * Alpaca broker desk (Broker System v2) — the `/brokers/alpaca` route target.
 * The shell owns the persona choice; each lens owns its own data and content.
 */
@Component({
  selector: 'app-alpaca-desk',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AlpacaDeployDrawerComponent,
    AlpacaAccountCardComponent,
    AlpacaOperatorLensComponent,
    AlpacaTraderLensComponent,
    TagModule,
  ],
  templateUrl: './alpaca-desk.component.html',
  styleUrl: './alpaca-desk.component.scss',
  host: { class: 'block h-full' },
  providers: [AlpacaOperatorLensDataService],
})
export class AlpacaDeskComponent {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly operatorData = inject(AlpacaOperatorLensDataService);
  private readonly queryParams = toSignal(this.route.queryParamMap, {
    initialValue: this.route.snapshot.queryParamMap,
  });

  protected readonly lens = linkedSignal<AlpacaDeskLens>(() =>
    lensFrom(this.queryParams().get('lens')) ?? storedLens() ?? 'trader',
  );
  protected readonly deployOpen = linkedSignal(() => this.queryParams().has('deploy'));

  constructor() {
    effect(() => {
      if (this.lens() === 'operator') this.operatorData.loadOnce();
    });
  }

  protected selectLens(lens: AlpacaDeskLens): void {
    if (lens === this.lens()) return;
    this.lens.set(lens);
    persistLens(lens);
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { lens },
      queryParamsHandling: 'merge',
    });
  }

  protected openDeploy(): void {
    this.deployOpen.set(true);
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { deploy: '', deployLens: 'trader' },
      queryParamsHandling: 'merge',
    });
  }

  protected closeDeploy(): void {
    this.deployOpen.set(false);
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { deploy: null, deployLens: null },
      queryParamsHandling: 'merge',
    });
  }

  protected onLensKeydown(event: KeyboardEvent): void {
    const nextLens =
      event.key === 'ArrowRight' || event.key === 'End'
        ? 'operator'
        : event.key === 'ArrowLeft' || event.key === 'Home'
          ? 'trader'
          : null;
    if (nextLens === null) return;
    event.preventDefault();
    this.selectLens(nextLens);
    if (!(event.currentTarget instanceof HTMLElement)) return;
    const target = event.currentTarget.parentElement?.querySelector(`[data-lens="${nextLens}"]`);
    if (target instanceof HTMLElement) target.focus();
  }
}
