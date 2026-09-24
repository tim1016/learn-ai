import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  input,
  resource,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { withCommand, withEntity, type ResourceTarget } from '../../../fleet/resource-target';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { PanelActionButtonComponent } from '../v2-panel/panel-action-button/panel-action-button.component';
import { BrokerV2PanelService } from '../v2-panel/lib/broker-v2-panel.service';
import type {
  BotCatalogView,
  BotPanelView,
  DutyOutcomeView,
  PanelActionTrigger,
} from '../v2-panel/lib/broker-v2-panel.types';
import { deriveActionRejection } from '../v2-panel/lib/panel-action-outcome';

type WarmupJoinView = NonNullable<BotPanelView['warmup_join']>;

/** How often, and for how long, a resumed bot is re-read until its warmup reports. */
const WARMUP_POLL_MS = 2_000;
const WARMUP_POLL_LIMIT = 45;

/** What the operator is shown for the bot they last resumed from this page. */
interface ResumeOutcome {
  readonly sid: string;
  readonly strategyLabel: string;
  readonly symbol: string;
  readonly message: string;
  readonly state: 'warming' | 'reported' | 'unreported' | 'refused_by_admission';
  readonly warmupJoin: WarmupJoinView | null;
  readonly dutyOutcome: DutyOutcomeView | null;
}

/**
 * Stopped bots on this account, resumable from the deploy page (#2314).
 *
 * A resumed bot warms on the bars its earlier runs kept and fills the hole
 * since they stopped from IBKR 1-minute history, or is refused when that hole
 * cannot be filled. Warmup runs after the Resume command returns, so this
 * section re-reads the bot until its panel reports how the join went, and
 * shows that — the filled window, or the refusal and what to do next — here,
 * where the operator pressed Resume.
 */
@Component({
  selector: 'app-deploy-resume-bots',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [PanelActionButtonComponent, RouterLink, TimestampDisplayComponent],
  templateUrl: './deploy-resume-bots.component.html',
  styleUrl: './deploy-resume-bots.component.scss',
})
export class DeployResumeBotsComponent {
  readonly target = input.required<ResourceTarget>();
  readonly accountId = input.required<string>();

  private readonly panelService = inject(BrokerV2PanelService);
  private destroyed = false;
  /** Bumped per Resume, so an older resume's poll stops writing once a newer one starts. */
  private resumeSeq = 0;

  protected readonly catalog = resource({
    params: () => this.target(),
    loader: ({ params }) => this.panelService.getCatalog(params),
  });

  protected readonly stoppedBots = computed<BotCatalogView[]>(() =>
    this.catalog.hasValue()
      ? this.catalog.value().filter((bot) => bot.row_action?.action_id === 'resume')
      : [],
  );

  protected readonly pendingSid = signal<string | null>(null);
  protected readonly outcome = signal<ResumeOutcome | null>(null);

  constructor() {
    inject(DestroyRef).onDestroy(() => {
      this.destroyed = true;
    });
  }

  protected botPath(sid: string): string[] {
    const target = this.target();
    return ['/brokers', target.broker, 'clerks', target.clerkId, 'accounts', this.accountId(), 'bots', sid];
  }

  protected async resume(bot: BotCatalogView, trigger: PanelActionTrigger): Promise<void> {
    if (this.pendingSid() !== null) return;
    const sid = bot.strategy_instance_id;
    const seq = ++this.resumeSeq;
    const shown = { sid, strategyLabel: bot.strategy_label, symbol: bot.symbol };
    this.pendingSid.set(sid);
    try {
      const command = withCommand(withEntity(this.target(), sid), 'bot_action', crypto.randomUUID());
      const result = await this.panelService.runBotAction(command, sid, trigger.action, trigger.reason);
      this.outcome.set({
        ...shown,
        message: result.message,
        state: 'warming',
        warmupJoin: null,
        dutyOutcome: null,
      });
      void this.followWarmup(sid, seq, result.recorded_at_ms);
    } catch (error) {
      this.outcome.set({
        ...shown,
        message: deriveActionRejection(error, `Could not resume ${sid}.`).message,
        state: 'refused_by_admission',
        warmupJoin: null,
        dutyOutcome: null,
      });
    } finally {
      this.pendingSid.set(null);
      this.catalog.reload();
    }
  }

  /** Re-read the resumed bot until its current run reports its warmup join or ends. */
  private async followWarmup(sid: string, seq: number, resumedAtMs: number): Promise<void> {
    for (let attempt = 0; attempt < WARMUP_POLL_LIMIT; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, WARMUP_POLL_MS));
      if (this.destroyed || seq !== this.resumeSeq) return;
      let panel: BotPanelView;
      try {
        panel = await this.panelService.getPanel(withEntity(this.target(), sid), sid);
      } catch {
        // One failed read is not an outcome; the next poll asks again, and
        // the loop's limit still reports "not reported" if none ever lands.
        continue;
      }
      if (this.destroyed || seq !== this.resumeSeq) return;
      const duty = panel.health.duty_outcome ?? null;
      const endedSinceResume =
        !panel.health.running && duty !== null && (duty.recorded_at_ms ?? 0) >= resumedAtMs;
      const join = panel.warmup_join ?? null;
      if (join !== null || endedSinceResume) {
        this.outcome.update((current) =>
          current === null || current.sid !== sid
            ? current
            : {
                ...current,
                state: 'reported',
                warmupJoin: join,
                dutyOutcome: endedSinceResume ? duty : null,
              },
        );
        this.catalog.reload();
        return;
      }
    }
    this.outcome.update((current) =>
      current === null || current.sid !== sid ? current : { ...current, state: 'unreported' },
    );
  }
}
