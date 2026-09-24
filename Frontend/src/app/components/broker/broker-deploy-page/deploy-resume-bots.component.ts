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

import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import {
  fencedTarget,
  laneFenceIsEnforceable,
  LANE_FENCE_REFRESH_FAILED_MESSAGE,
  LANE_FENCE_UNENFORCEABLE_MESSAGE,
  type LaneFence,
} from '../../../fleet/lane-fence';
import { withCommand, withEntity, type ResourceTarget } from '../../../fleet/resource-target';
import { AssetIdentityComponent } from '../../../shared/asset-identity';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import { PanelActionButtonComponent } from '../v2-panel/panel-action-button/panel-action-button.component';
import { BrokerV2PanelService } from '../v2-panel/lib/broker-v2-panel.service';
import type {
  BotCatalogView,
  BotPanelView,
  DutyOutcomeView,
  PanelAction,
  PanelActionTrigger,
} from '../v2-panel/lib/broker-v2-panel.types';
import { deriveActionRejection } from '../v2-panel/lib/panel-action-outcome';

type WarmupJoinView = NonNullable<BotPanelView['warmup_join']>;

/** How often, and for how long, a resumed bot is re-read until its warmup reports. */
const WARMUP_POLL_MS = 2_000;
const WARMUP_POLL_LIMIT = 45;

/** A stopped bot, the Resume its panel presents, and the join its last run recorded. */
interface ResumableBot {
  readonly bot: BotCatalogView;
  readonly resume: PanelAction;
  readonly priorJoinRunId: string | null;
}

/** What the operator is shown for the bot they last resumed from this page. */
interface ResumeOutcome {
  readonly sid: string;
  readonly strategyLabel: string;
  readonly symbol: string;
  readonly message: string;
  /** `rejected`: the Resume command itself was refused or failed; nothing ran.
   * `unreadable`: every re-read of the bot failed, so this page cannot say. */
  readonly state: 'warming' | 'reported' | 'unreported' | 'unreadable' | 'rejected';
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
  imports: [AssetIdentityComponent, PanelActionButtonComponent, RouterLink, TimestampDisplayComponent],
  templateUrl: './deploy-resume-bots.component.html',
  styleUrl: './deploy-resume-bots.component.scss',
})
export class DeployResumeBotsComponent {
  /** Live for reads; a command is minted only through `fence`. */
  readonly target = input.required<ResourceTarget>();
  readonly accountId = input.required<string>();
  /** The desk's binding-generation fence, frozen at desk-render time (#2106):
   * a Resume must conflict rather than follow the operator onto a rebound lane. */
  readonly fence = input.required<LaneFence>();

  private readonly panelService = inject(BrokerV2PanelService);
  private readonly fleetDirectory = inject(FleetDirectoryService);
  private destroyed = false;
  /** Bumped per Resume, so an older resume's poll stops writing once a newer one starts. */
  private resumeSeq = 0;

  /**
   * Every stopped bot with the Resume its own panel presents. The roster row
   * carries only a recovery command, never a routine Resume, so the action --
   * with its token, blockers and confirmation -- is read from the bot's panel,
   * the one surface that presents it.
   */
  protected readonly catalog = resource({
    params: () => this.target(),
    loader: async ({ params }): Promise<ResumableBot[]> => {
      const stopped = (await this.panelService.getCatalog(params)).filter(
        (bot) => !bot.running && bot.phase !== 'RETIRED',
      );
      const panels = await Promise.all(
        stopped.map((bot) =>
          this.panelService.getPanel(withEntity(params, bot.strategy_instance_id), bot.strategy_instance_id),
        ),
      );
      return stopped.flatMap((bot, index) => {
        const panel = panels[index];
        const resume = panel.actions.find((action) => action.action_id === 'resume');
        return resume === undefined
          ? []
          : [{ bot, resume, priorJoinRunId: panel.warmup_join?.run_id ?? null }];
      });
    },
  });

  protected readonly stoppedBots = computed<ResumableBot[]>(() =>
    this.catalog.hasValue() ? this.catalog.value() : [],
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

  protected async resume({ bot, priorJoinRunId }: ResumableBot, trigger: PanelActionTrigger): Promise<void> {
    if (this.pendingSid() !== null) return;
    const sid = bot.strategy_instance_id;
    const seq = ++this.resumeSeq;
    const shown = { sid, strategyLabel: bot.strategy_label, symbol: bot.symbol };
    if (!laneFenceIsEnforceable(this.fence())) {
      this.outcome.set(this.settled(shown, LANE_FENCE_UNENFORCEABLE_MESSAGE, 'rejected'));
      return;
    }
    this.pendingSid.set(sid);
    try {
      const command = withCommand(
        withEntity(fencedTarget(this.target(), this.fence()), sid),
        'bot_action',
        crypto.randomUUID(),
      );
      const result = await this.panelService.runBotAction(command, sid, trigger.action, trigger.reason);
      this.outcome.set(this.settled(shown, result.message, 'warming'));
      void this.followWarmup(sid, seq, result.recorded_at_ms, priorJoinRunId);
    } catch (error) {
      const rejection = deriveActionRejection(error, `Could not resume ${sid}.`);
      this.outcome.set(this.settled(shown, rejection.message, 'rejected'));
      // A stale-generation refusal proves the fence shown was wrong; refresh
      // so the next Resume is minted against a lane the operator has seen.
      if (rejection.reasonCode === 'clerk_binding_generation_conflict') {
        void this.fleetDirectory.refresh().catch(() => {
          if (seq !== this.resumeSeq) return;
          this.outcome.update((current) =>
            current === null ? current : { ...current, message: LANE_FENCE_REFRESH_FAILED_MESSAGE },
          );
        });
      }
    } finally {
      this.pendingSid.set(null);
      this.catalog.reload();
    }
  }

  protected readonly outcomeRefused = computed(() => {
    const result = this.outcome();
    return (
      result !== null &&
      (result.state === 'rejected' ||
        result.warmupJoin?.state === 'refused' ||
        result.dutyOutcome !== null)
    );
  });

  private settled(
    shown: Pick<ResumeOutcome, 'sid' | 'strategyLabel' | 'symbol'>,
    message: string,
    state: ResumeOutcome['state'],
  ): ResumeOutcome {
    return { ...shown, message, state, warmupJoin: null, dutyOutcome: null };
  }

  /** Re-read the resumed bot until its current run reports its warmup join or ends. */
  private async followWarmup(
    sid: string,
    seq: number,
    resumedAtMs: number,
    priorJoinRunId: string | null,
  ): Promise<void> {
    let readAny = false;
    for (let attempt = 0; attempt < WARMUP_POLL_LIMIT; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, WARMUP_POLL_MS));
      if (this.destroyed || seq !== this.resumeSeq) return;
      let panel: BotPanelView;
      try {
        panel = await this.panelService.getPanel(withEntity(this.target(), sid), sid);
      } catch {
        // One failed read is not an outcome; the next poll asks again. If no
        // read ever lands the section says it could not read the bot, never
        // that the bot has not reported.
        continue;
      }
      readAny = true;
      if (this.destroyed || seq !== this.resumeSeq) return;
      const duty = panel.health.duty_outcome ?? null;
      const endedSinceResume =
        !panel.health.running && duty !== null && (duty.recorded_at_ms ?? 0) >= resumedAtMs;
      // A coalesced read that started before Resume can still hand back the
      // stopped run's join; only a join of another run is this resume's.
      const candidate = panel.warmup_join ?? null;
      const join = candidate !== null && candidate.run_id !== priorJoinRunId ? candidate : null;
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
      current === null || current.sid !== sid
        ? current
        : { ...current, state: readAny ? 'unreported' : 'unreadable' },
    );
  }
}
