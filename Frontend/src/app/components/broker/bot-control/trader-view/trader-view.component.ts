import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type {
  BotLifecycleActionId,
  LiveInstanceStatus,
} from '../../../../api/live-instances.types';
import type { OperatorMove } from '../../../../api/operator-blocker.types';
import type { PresentedAction } from '../lib/suggested-action-renderer';
import { resolveVerdictCardModel } from '../lib/verdict-card-model';
import { MARKET_PHASES, resolveTraderViewModel } from './trader-view.model';

@Component({
  selector: 'app-trader-view',
  templateUrl: './trader-view.component.html',
  styleUrl: './trader-view.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class TraderViewComponent {
  readonly status = input.required<LiveInstanceStatus>();
  readonly busy = input<boolean>(false);
  readonly renderedRemediation = input<PresentedAction | null>(null);

  readonly lifecycleAction = output<BotLifecycleActionId>();
  readonly remediationInvoked = output();
  readonly crashRecoveryRequested = output();
  readonly blockerMoveRequested = output<OperatorMove>();
  readonly operationsRequested = output();

  readonly marketPhases = MARKET_PHASES;
  readonly model = computed(() =>
    resolveTraderViewModel(this.status(), this.renderedRemediation()),
  );

  invokePrimaryAction(): void {
    if (this.busy()) return;
    const verb = resolveVerdictCardModel(this.status()).verb;
    switch (verb.kind) {
      case 'lifecycle': this.lifecycleAction.emit(verb.action.id); break;
      case 'blocker_move': this.blockerMoveRequested.emit(verb.move); break;
      case 'remediation': this.remediationInvoked.emit(); break;
      case 'crash_recovery': this.crashRecoveryRequested.emit(); break;
      case 'evidence': this.operationsRequested.emit(); break;
      case 'none': break;
    }
  }

  isCurrentMarketPhase(phase: string): boolean {
    return this.model().marketPhase === phase;
  }
}
