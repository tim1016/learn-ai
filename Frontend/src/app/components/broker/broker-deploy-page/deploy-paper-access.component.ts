import { HttpErrorResponse } from "@angular/common/http";
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
} from "@angular/core";
import { Button } from "primeng/button";

import { TimestampDisplayPipe } from "../../../shared/timestamp";
import {
  BrokerV2PanelService,
  type DeployBotStrategy,
  type PaperAccessPlan,
} from "../v2-panel/lib/broker-v2-panel.service";
import { type ResourceTarget, withCommand } from '../../../fleet/resource-target';
import { laneFenceIsEnforceable, LANE_FENCE_UNENFORCEABLE_MESSAGE } from '../../../fleet/lane-fence';

const UI_ACTIVATION_REASON = "Enable Paper access from the Alpaca Deploy page.";

interface PaperAccessFailure {
  message: string;
  explanation: string | null;
  nextAction: string | null;
}

type PaperAccessFlow =
  | { kind: "idle" }
  | { kind: "preparing" }
  | { kind: "review"; plan: PaperAccessPlan; target: ResourceTarget; strategyKey: string }
  | { kind: "confirming"; plan: PaperAccessPlan; target: ResourceTarget; strategyKey: string }
  | { kind: "complete" }
  | {
    kind: "error";
    failure: PaperAccessFailure;
    retry: { plan: PaperAccessPlan; target: ResourceTarget; strategyKey: string } | null;
  };

/** Two-step account approval for one sealed Signal Program. */
@Component({
  selector: "app-deploy-paper-access",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Button, TimestampDisplayPipe],
  host: { class: "block min-w-0" },
  templateUrl: "./deploy-paper-access.component.html",
})
export class DeployPaperAccessComponent {
  readonly accountId = input.required<string>();
  readonly target = input.required<ResourceTarget>();
  readonly strategy = input.required<DeployBotStrategy>();
  /**
   * The broker world this account's grant is worded for: `Paper` on a paper
   * account, `Shadow` on a live one held by the Shadow Account Authority
   * (ADR 0059 D2). `Live` on one custodied by its live authority. The
   * backend enum stays `paper_access_state` — that is the grant's identity,
   * not prose — but the prose must not call a real-money account's grant
   * "Paper".
   */
  readonly modeLabel = input.required<"Paper" | "Shadow" | "Live">();
  readonly accessChanged = output();

  private readonly panelService = inject(BrokerV2PanelService);
  private readonly identity = computed(
    () => {
      const target = this.target();
      return [
        target.broker,
        target.clerkId,
        target.accountId,
        target.bindingGeneration,
        target.routingEpoch,
        this.strategy().strategy_key,
      ].join("\u0000");
    },
  );
  private lastIdentity = "";

  protected readonly flow = signal<PaperAccessFlow>({ kind: "idle" });

  // Assembled here rather than in the template: every control keeps an
  // accessible name, and one author decides how the world word reads.
  protected readonly reviewLabel = computed(
    () => `Review & enable ${this.modeLabel()}`,
  );
  protected readonly reviewRegionLabel = computed(
    () => `Review ${this.modeLabel()} access`,
  );
  protected readonly enableLabel = computed(
    () => `Enable ${this.modeLabel()} access`,
  );

  constructor() {
    effect(() => {
      const identity = this.identity();
      if (identity === this.lastIdentity) return;
      this.lastIdentity = identity;
      this.flow.set({ kind: "idle" });
    });
  }

  protected async prepare(): Promise<void> {
    const strategy = this.strategy();
    if (strategy.paper_access_state !== "available") return;
    const identity = this.identity();
    const laneTarget = this.target();
    // `target` arrives already frozen at drawer-open (#2106) — the drawer
    // captures it once, before this component even exists, and never
    // re-derives it from a live directory read while open. A cold directory
    // at that moment still freezes a null generation, though, and
    // `commandContextOf` sends no generation check at all for one — refuse
    // rather than dispatch blind (see `lane-fence.ts`'s doc on why a null
    // fence is not a safe default).
    if (!laneFenceIsEnforceable({
      bindingGeneration: laneTarget.bindingGeneration,
      routingEpoch: laneTarget.routingEpoch,
    })) {
      this.flow.set({
        kind: "error",
        failure: { message: LANE_FENCE_UNENFORCEABLE_MESSAGE, explanation: null, nextAction: null },
        retry: null,
      });
      return;
    }
    // The review and its confirmation are one durable interaction. Capture
    // the lane and key before the first await so a later route reuse cannot
    // send the reviewed plan through a newly-bound Clerk.
    const target = withCommand(laneTarget, 'deploy', crypto.randomUUID());
    this.flow.set({ kind: "preparing" });
    try {
      const plan = await this.panelService.preparePaperAccess(
        target,
        strategy.strategy_key,
        UI_ACTIVATION_REASON,
      );
      if (identity !== this.identity()) return;
      this.flow.set({ kind: "review", plan, target, strategyKey: strategy.strategy_key });
    } catch (error) {
      if (identity !== this.identity()) return;
      this.flow.set({ kind: "error", failure: this.toFailure(error), retry: null });
    }
  }

  protected async confirm(): Promise<void> {
    const review = this.flow();
    if (review.kind !== "review") return;
    const identity = this.identity();
    this.flow.set({
      kind: "confirming",
      plan: review.plan,
      target: review.target,
      strategyKey: review.strategyKey,
    });
    try {
      await this.panelService.confirmPaperAccess(
        review.target,
        review.strategyKey,
        review.plan,
      );
      if (identity !== this.identity()) return;
      this.flow.set({ kind: "complete" });
      this.accessChanged.emit();
    } catch (error) {
      if (identity !== this.identity()) return;
      this.flow.set({
        kind: "error",
        failure: this.toFailure(error),
        retry: { plan: review.plan, target: review.target, strategyKey: review.strategyKey },
      });
    }
  }

  protected retry(): void {
    const failed = this.flow();
    if (failed.kind !== "error" || failed.retry === null) {
      void this.prepare();
      return;
    }
    // A confirm can fail after reaching the coordinator. Replaying the exact
    // frozen target lets its durable key answer that uncertainty instead of
    // converting the retry into a second access command.
    this.flow.set({ kind: "review", ...failed.retry });
    void this.confirm();
  }

  protected cancel(): void {
    this.flow.set({ kind: "idle" });
  }

  private toFailure(error: unknown): PaperAccessFailure {
    if (error instanceof HttpErrorResponse) {
      const detail = error.error?.detail as
        | {
            message?: string;
            why?: string | null;
            next_action?: string | null;
          }
        | undefined;
      if (detail?.message) {
        return {
          message: detail.message,
          explanation: detail.why ?? null,
          nextAction: detail.next_action ?? null,
        };
      }
    }
    return {
      message: "Paper access could not be reviewed.",
      explanation: "The data plane did not return a current approval plan.",
      nextAction: "Check connectivity, then try the review again.",
    };
  }
}
