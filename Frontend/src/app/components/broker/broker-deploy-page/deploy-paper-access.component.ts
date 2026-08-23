import { DatePipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
} from '@angular/core';

import {
  BrokerV2PanelService,
  type DeployBotStrategy,
  type PaperAccessPlan,
} from '../v2-panel/lib/broker-v2-panel.service';

const UI_ACTIVATION_REASON = 'Enable Paper access from the Alpaca Deploy page.';

interface PaperAccessFailure {
  message: string;
  explanation: string | null;
  nextAction: string | null;
}

type PaperAccessFlow =
  | { kind: 'idle' }
  | { kind: 'preparing' }
  | { kind: 'review'; plan: PaperAccessPlan }
  | { kind: 'confirming'; plan: PaperAccessPlan }
  | { kind: 'complete' }
  | { kind: 'error'; failure: PaperAccessFailure };

/** Two-step account approval for one sealed Signal Program. */
@Component({
  selector: 'app-deploy-paper-access',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DatePipe],
  templateUrl: './deploy-paper-access.component.html',
  styleUrl: './deploy-paper-access.component.scss',
})
export class DeployPaperAccessComponent {
  readonly accountId = input.required<string>();
  readonly strategy = input.required<DeployBotStrategy>();
  readonly accessChanged = output();

  private readonly panelService = inject(BrokerV2PanelService);
  private readonly identity = computed(
    () => `${this.accountId()}\u0000${this.strategy().strategy_key}`,
  );
  private lastIdentity = '';

  protected readonly flow = signal<PaperAccessFlow>({ kind: 'idle' });

  constructor() {
    effect(() => {
      const identity = this.identity();
      if (identity === this.lastIdentity) return;
      this.lastIdentity = identity;
      this.flow.set({ kind: 'idle' });
    });
  }

  protected async prepare(): Promise<void> {
    const strategy = this.strategy();
    if (strategy.paper_access_state !== 'disabled') return;
    const identity = this.identity();
    this.flow.set({ kind: 'preparing' });
    try {
      const plan = await this.panelService.preparePaperAccess(
        'alpaca',
        this.accountId(),
        strategy.strategy_key,
        UI_ACTIVATION_REASON,
      );
      if (identity !== this.identity()) return;
      this.flow.set({ kind: 'review', plan });
    } catch (error) {
      if (identity !== this.identity()) return;
      this.flow.set({ kind: 'error', failure: this.toFailure(error) });
    }
  }

  protected async confirm(plan: PaperAccessPlan): Promise<void> {
    const identity = this.identity();
    this.flow.set({ kind: 'confirming', plan });
    try {
      await this.panelService.confirmPaperAccess(
        'alpaca',
        this.accountId(),
        this.strategy().strategy_key,
        plan,
      );
      if (identity !== this.identity()) return;
      this.flow.set({ kind: 'complete' });
      this.accessChanged.emit();
    } catch (error) {
      if (identity !== this.identity()) return;
      this.flow.set({ kind: 'error', failure: this.toFailure(error) });
    }
  }

  protected cancel(): void {
    this.flow.set({ kind: 'idle' });
  }

  private toFailure(error: unknown): PaperAccessFailure {
    if (error instanceof HttpErrorResponse) {
      const detail = error.error?.detail as {
        message?: string;
        why?: string | null;
        next_action?: string | null;
      } | undefined;
      if (detail?.message) {
        return {
          message: detail.message,
          explanation: detail.why ?? null,
          nextAction: detail.next_action ?? null,
        };
      }
    }
    return {
      message: 'Paper access could not be reviewed.',
      explanation: 'The data plane did not return a current approval plan.',
      nextAction: 'Check connectivity, then try the review again.',
    };
  }
}
