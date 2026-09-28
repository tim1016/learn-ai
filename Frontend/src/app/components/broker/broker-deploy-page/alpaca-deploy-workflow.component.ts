import { HttpErrorResponse } from '@angular/common/http';
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  Injector,
  afterNextRender,
  computed,
  effect,
  inject,
  input,
  resource,
  signal,
  untracked,
  viewChild,
} from '@angular/core';
import {
  FormField,
  form,
  max,
  min,
  pattern,
  required,
  validate,
} from '@angular/forms/signals';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';

import {
  BrokerV2PanelService,
  committedReceipt,
  uncommittedClaim,
  type BotDeployPrefill,
  type BudgetDeployReceipt,
  type DeployBotBody,
  type DeployBotStrategy,
  type DeployBotView,
  type DeployExecutionMode,
  type DeploySubmissionBody,
  type DeploySubmissionUncommitted,
  type DeploymentBudgetInput,
  type DeployStrategyParamsSchema,
  type QualifiedDeployConfiguration,
  type RunAdmissionDecision,
} from '../v2-panel/lib/broker-v2-panel.service';
import { laneKey, type ResourceTarget, withAccount, withCommand } from '../../../fleet/resource-target';
import {
  LANE_FENCE_UNENFORCEABLE_MESSAGE,
  laneFenceIsEnforceable,
  fencedTarget,
  type LaneFence,
} from '../../../fleet/lane-fence';
import {
  DEPLOY_AGAIN_QUERY_PARAM,
  accountWorkspaceBotRoute,
  accountWorkspaceTabRoute,
  type AccountWorkspaceLink,
} from '../../../fleet/account-workspace';
import { extractServerMessage } from '../operation-error';
import { DeployBindingStripComponent } from './deploy-binding-strip.component';
import {
  DeployConfirmStepComponent,
  type DeployBlocker,
  type DeployError,
} from './deploy-confirm-step.component';
import {
  DeployDraftStore,
  EMPTY_DEPLOY_SETTINGS,
  canonicalJson,
  freshDeployDraft,
  type DeployDraft,
  type DeployStepEditing,
  type DeployTicketSettings,
} from './deploy-draft.store';
import {
  DeployExecutionSectionComponent,
  type DeploySizingPreset,
} from './deploy-execution-section.component';
import { DeployLaunchReceiptComponent } from './deploy-launch-receipt.component';
import { DeployMoneyStepComponent, budgetReviewContext, type MoneyReview } from './deploy-money-step.component';
import { DeployParametersSectionComponent } from './deploy-parameters-section.component';
import { DeployPaperAccessComponent } from './deploy-paper-access.component';
import { DeployEvidenceOverrideComponent } from './deploy-evidence-override.component';
import { DeployStepComponent } from './deploy-step.component';
import { FleetDirectoryService } from '../../../fleet/fleet-directory.service';
import { SymbolPickerComponent } from '../../../shared/symbol-picker/symbol-picker.component';

import { sameAlpacaAccount } from '../../../services/alpaca-account-identity';

/** A bot id as the backend's path-safe validator admits it (Deploy again's `?from=`). */
const INSTANCE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
/** A submission key as the backend admits it (`?submission=`). */
const SUBMISSION_KEY_RE = /^[A-Za-z0-9_-]{8,64}$/;
const SYMBOL_RE = /^[A-Za-z][A-Za-z0-9.-]{0,11}$/;

/** The recovery hint a Deploy writes before it is sent, so a reload can read
 * what that submission recorded instead of starting a second bot. */
const SUBMISSION_PARAM = 'submission';

/** The Deploy refusals that settle nothing about the key's first Deploy: a
 * second copy while it is being sent, and the key resent with other
 * settings. Like a lost response, each leaves its outcome to be read. */
const SUBMISSION_UNSETTLED_REASONS: ReadonlySet<string> = new Set([
  'deploy_submission_in_flight',
  'deploy_submission_settings_conflict',
]);

/** What Confirm says about a Deploy that no receipt answered for. */
function deployNotice(
  outcome: DeployError['outcome'],
  title: string,
  message: string,
  explanation: string | null = null,
  nextAction: string | null = null,
): DeployError {
  return { outcome, title, message, explanation, nextAction, receiptId: null, recordedAtMs: null };
}

/** A Deploy that got no answer, in the owner's words. */
const UNKNOWN_OUTCOME = deployNotice(
  'unknown',
  'Outcome unknown',
  'No answer came back from Deploy, so whether the bot started is not known yet.',
  'Checking its status, or pressing Deploy again, never starts a second bot.',
  'Check deployment status.',
);

const NOT_COMMITTED_MESSAGE = 'No Deploy was committed for this submission. Nothing was set aside and nothing started.';

/**
 * The symbol input fires per keystroke. Scoping the readiness fetch to every
 * intermediate prefix would burn a request per character and let a stale
 * response land after a newer one; one settle beat is enough.
 */
const SYMBOL_SCOPE_DEBOUNCE_MS = 400;

function sameParameterValues(
  left: Readonly<Record<string, unknown>>,
  right: Readonly<Record<string, unknown>>,
): boolean {
  const leftKeys = Object.keys(left);
  return leftKeys.length === Object.keys(right).length
    && leftKeys.every((key) => Object.hasOwn(right, key) && Object.is(left[key], right[key]));
}

/** The form's model: the draftable settings plus the evidence-only
 * acknowledgement, which is consent and so never kept in a draft. */
interface AlpacaDeployTicket extends DeployTicketSettings {
  overrideAcknowledged: boolean;
  overrideReason: string;
}

function ticketOf(settings: DeployTicketSettings): AlpacaDeployTicket {
  return { ...settings, overrideAcknowledged: false, overrideReason: '' };
}

function settingsOf(ticket: AlpacaDeployTicket): DeployTicketSettings {
  return {
    exitAllowanceBps: ticket.exitAllowanceBps,
    bandMultiple: ticket.bandMultiple,
    spreadCapBps: ticket.spreadCapBps,
    strategyKey: ticket.strategyKey,
    symbol: ticket.symbol,
    sizingPreset: ticket.sizingPreset,
    quantity: ticket.quantity,
    executionMode: ticket.executionMode,
    allowCarryover: ticket.allowCarryover,
    parameters: ticket.parameters,
  };
}

interface ValidationScopeSeed {
  strategyKey: DeployBotStrategy['strategy_key'];
  symbol: string;
  parameters: Readonly<Record<string, unknown>>;
  golden: boolean;
}

function validationScopeSeed(strategy: DeployBotStrategy): ValidationScopeSeed {
  return {
    strategyKey: strategy.strategy_key,
    symbol: strategy.validation_case_symbol,
    parameters: strategy.validation_case_parameters,
    golden: strategy.golden_validation_scope,
  };
}

function sameValidationScope(left: ValidationScopeSeed | null, right: ValidationScopeSeed): boolean {
  return left !== null
    && left.strategyKey === right.strategyKey
    && left.symbol === right.symbol
    && left.golden === right.golden
    && sameParameterValues(left.parameters, right.parameters);
}

const OVERRIDE_REASON_MIN_LENGTH = 10;

/** The owner-facing name of each world, as the permission and summaries word it. */
const WORLD_LABELS: Readonly<Record<'paper' | 'shadow' | 'live', 'Paper' | 'Shadow' | 'Live'>> = {
  paper: 'Paper',
  shadow: 'Shadow',
  live: 'Live',
};

interface DeploySubmissionReadiness {
  canSubmit: boolean;
  guidance: string;
}

interface FrozenDeployCommand {
  readonly context: string;
  readonly ticketKey: string;
  readonly routeKey: string;
  readonly target: ResourceTarget;
}

type DeploySubmission = DeploySubmissionBody & { budget: DeploymentBudgetInput };

/**
 * Deploy (PRD #2560 D8/D9): four steps on one page — What → How → Money →
 * Confirm.
 *
 * What and How fold to one line once complete, and stay open (with Done)
 * once the owner is working in them, so a step never folds away under the
 * keyboard. Money renders only the backend's previewed `money_after`, and
 * consent binds to the preview's review token and Live phrase. The backend
 * names the bot (#2551): each Deploy carries an opaque submission key, kept
 * with the settings it was first sent with, so a retry — a double click, a
 * lost response, a reload — returns the same bot. The form is kept per
 * account for the session (H9); Deploy again (`?from=<sid>`) pre-fills
 * everything but money and consent.
 */
@Component({
  selector: 'app-alpaca-deploy-workflow',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    DeployBindingStripComponent,
    DeployConfirmStepComponent,
    DeployEvidenceOverrideComponent,
    DeployExecutionSectionComponent,
    DeployLaunchReceiptComponent,
    DeployMoneyStepComponent,
    DeployPaperAccessComponent,
    DeployParametersSectionComponent,
    DeployStepComponent,
    FormField,
    RouterLink,
    SymbolPickerComponent,
  ],
  templateUrl: './alpaca-deploy-workflow.component.html',
  styleUrl: './alpaca-deploy-workflow.component.scss',
})
export class AlpacaDeployWorkflowComponent {
  /** Reads follow the current account; each submitted command freezes its own target. */
  protected readonly deployTarget = (accountId: string) => withAccount(fencedTarget(this.target(), this.fence()), accountId);

  readonly accountId = input.required<string>();
  /** Current page account; each submitted attempt freezes its own target. */
  readonly target = input.required<ResourceTarget>();
  readonly fence = input.required<LaneFence>();
  readonly laneReviewRequired = input(false);

  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly drafts = inject(DeployDraftStore);
  private termsSeeded = false;
  private readonly panelService = inject(BrokerV2PanelService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);
  private readonly injector = inject(Injector);

  /** The `?strategy=`, `?from=` and `?submission=` deep links. */
  private readonly queryParams = toSignal(this.route.queryParamMap, {
    initialValue: this.route.snapshot.queryParamMap,
  });

  protected readonly submitting = signal(false);
  protected readonly submitError = signal<DeployError | null>(null);
  protected readonly invalidParameterFields = signal<ReadonlySet<string>>(new Set());
  protected readonly receipt = signal<BudgetDeployReceipt | null>(null);
  protected readonly admissionDecision = signal<RunAdmissionDecision | null>(null);
  /** Retained only while the exact deploy intent has no terminal receipt. */
  private readonly frozenCommand = signal<FrozenDeployCommand | null>(null);
  /** Submission stays blocked until the changed account has been refreshed. */
  protected readonly laneConflict = signal(false);
  /** A cold directory has no generation the coordinator can enforce.
   * Refuse submission until both current lane fences are known (#2068). */
  protected readonly laneUnenforceable = computed(
    () =>
      !laneFenceIsEnforceable(this.fence()),
  );

  private readonly receiptPanel = viewChild(DeployLaunchReceiptComponent);
  private readonly confirmStep = viewChild(DeployConfirmStepComponent);

  /**
   * The symbol the readiness fetch is scoped to, or null for the
   * account-level view. Deliberately not a resource *dependency*: the ticket
   * is seeded from the loaded view, so keying the resource on the symbol
   * would loop (load → seed → reload → …) and strand the pane on its loading
   * state. Written only by `scheduleSymbolScope`, after the debounce and
   * after the guard that recognizes a symbol the gates already describe.
   */
  private readonly scopedSymbol = signal<string | null>(null);
  private symbolScopeTimer: ReturnType<typeof setTimeout> | null = null;

  /**
   * `params` stays keyed on the account alone. The scoped symbol is read
   * untracked inside the loader and applied by an explicit `reload()`, which
   * — unlike a `params` change — keeps the previous value on screen instead
   * of dropping it and flickering the pane back to its loading state.
   */
  protected readonly deployView = resource({
    params: () => this.accountId().trim(),
    loader: async ({ params, abortSignal }) => {
      const symbol = untracked(this.scopedSymbol);
      const view = await this.panelService.getDeployView(
        this.deployTarget(params),
        symbol ?? undefined,
        untracked(() => this.exitTerms() ?? undefined),
      );
      // `getDeployView` wraps `firstValueFrom(http.get)`, which no `reload()`
      // can cancel, so a superseded request still answers — and, being
      // slower, can answer last. Letting its write win would leave the
      // retained record claiming a scope the operator has already left.
      if (!abortSignal.aborted) {
        this.lastLoadedView.set({ accountId: params, symbol, view });
      }
      return view;
    },
  });

  /**
   * The last readiness view that actually loaded, with the account AND symbol
   * it describes. Retaining it lets a failed *refresh* degrade to an explicit
   * staleness banner over the checks the owner already has, instead of
   * collapsing the page — and the whole form with it — back to an error.
   *
   * The symbol is half the identity, not decoration. Without it a SPY-scoped
   * set of checks silently backed a QQQ ticket; with it, a mismatch is a
   * condition the page can name and refuse to deploy on.
   */
  private readonly lastLoadedView = signal<{
    accountId: string;
    symbol: string | null;
    view: DeployBotView;
  } | null>(null);

  protected readonly currentView = computed(() => {
    if (this.deployView.hasValue()) return this.deployView.value();
    const retained = this.lastLoadedView();
    return retained?.accountId === this.accountId().trim() ? retained.view : null;
  });

  /**
   * True when what is on screen is not current admission truth for this
   * ticket: a refresh failed, or the checks that loaded describe a different
   * symbol than the one now scoped. Either way nothing may be deployed on
   * them — a `role="alert"` staleness banner beside a live Deploy button is
   * the worst of both.
   */
  private readonly admissionIsStale = computed(() => {
    const retained = this.lastLoadedView();
    if (retained === null) return false;
    return retained.symbol !== this.scopedSymbol() || this.deployView.error() !== undefined;
  });

  /**
   * The owner-facing half of the above: the sentence that names what is on
   * screen and what it is not. Silently serving stale checks is what makes a
   * deployment decision unsafe.
   */
  protected readonly stalenessNotice = computed<string | null>(() => {
    const retained = this.lastLoadedView();
    if (retained === null || this.currentView() === null) return null;
    if (!this.admissionIsStale()) return null;
    // A refresh still in flight is refreshing, not stale. Raising an alert
    // for every settled keystroke would teach the owner to ignore it;
    // `admissionIsStale` still refuses the deploy meanwhile.
    if (this.deployView.isLoading()) return null;
    const shown = retained.symbol ?? 'this account';
    const wanted = this.scopedSymbol() ?? 'this account';
    return wanted === shown
      ? `Deploy checks for ${shown} could not be refreshed. ` +
        'The checks shown are the last that loaded.'
      : `Deploy checks for ${wanted} could not be refreshed. ` +
        `The checks shown describe ${shown}.`;
  });

  protected readonly ticket = signal<AlpacaDeployTicket>(ticketOf(EMPTY_DEPLOY_SETTINGS));
  /** The owner's typed dollar amount (Money). Kept in the draft; its review
   * and any typed consent never are. */
  protected readonly amount = signal('');
  /** The opaque idempotency input of the next Deploy (#2551). */
  private readonly submissionKey = signal<string>(crypto.randomUUID());
  /** The settings `submissionKey` was first sent with; a Deploy with any
   * other settings mints a new key rather than reuse one the backend would
   * refuse as a conflict — unless that Deploy's outcome is unknown. */
  private readonly submittedContent = signal<string | null>(null);
  /** A Deploy went out under `submissionKey` and no answer settled it (a lost
   * response, or "already being sent"). The key is kept whatever the form now
   * says until its status read answers (PRD #2560 stories 63, 66). */
  protected readonly outcomeUnknown = signal(false);
  protected readonly checkingStatus = signal(false);
  /** The `?submission=` this page last wrote, just before sending it. */
  private readonly writtenKey = signal<string | null>(null);
  protected readonly editing = signal<DeployStepEditing>({ what: false, how: false });
  /** Deploy again's display-only lineage: the bot this one follows. */
  protected readonly replaces = signal<string | null>(null);
  /** The Money step's accepted review for the amount on screen. */
  protected readonly moneyReview = signal<MoneyReview | null>(null);
  /** Live's typed phrase. Never kept: a new review clears it. */
  protected readonly liveConsent = signal('');

  private readonly overrideReasonTouched = signal(false);
  private lastValidationScope: ValidationScopeSeed | null = null;
  private lastRequestedStrategyKey: string | null = null;

  /** One draft per account, whatever the lane's routing epoch. */
  private readonly draftKey = computed(() => {
    const target = this.target();
    return [target.broker, target.clerkId, this.accountId().trim().toLowerCase()].join('\u0000');
  });
  /** The account whose draft the form holds; a recovery read waits for it,
   * so it never mistakes the draft's own Deploy for someone else's. */
  private readonly restoredDraftKey = signal<string | null>(null);

  protected readonly ticketForm = form(this.ticket, (ticket) => {
    required(ticket.exitAllowanceBps);
    min(ticket.exitAllowanceBps, 0);
    max(ticket.exitAllowanceBps, 9999.999);
    required(ticket.bandMultiple);
    min(ticket.bandMultiple, 1);
    max(ticket.bandMultiple, 10);
    required(ticket.spreadCapBps);
    min(ticket.spreadCapBps, 1);
    max(ticket.spreadCapBps, 1000);
    required(ticket.strategyKey, { message: 'Choose a deployment strategy.' });
    required(ticket.symbol, { message: 'Enter the strategy signal symbol.' });
    pattern(ticket.symbol, SYMBOL_RE, { message: 'Enter a valid stock symbol.' });
    min(ticket.quantity, 1, { message: 'Quantity must be at least one whole share.' });
    max(ticket.quantity, 100, { message: 'Quantity cannot exceed 100 shares.' });
    validate(ticket.quantity, ({ value }) =>
      Number.isInteger(value())
        ? undefined
        : { kind: 'whole-share-quantity', message: 'Quantity must be a whole number.' },
    );
  });

  private exitTerms(): DeployBotBody['exit_terms'] | null {
    const ticket = this.ticket();
    if (ticket.exitAllowanceBps === null || ticket.bandMultiple === null || ticket.spreadCapBps === null
      || this.ticketForm.exitAllowanceBps().invalid() || this.ticketForm.bandMultiple().invalid()
      || this.ticketForm.spreadCapBps().invalid()) return null;
    return { exit_allowance_bps: ticket.exitAllowanceBps, band_multiple: ticket.bandMultiple,
      spread_cap_bps: ticket.spreadCapBps };
  }

  protected refreshExitTerms(): void {
    this.admissionDecision.set(null);
    if (this.exitTerms() !== null) this.deployView.reload();
  }

  protected readonly selectedStrategy = computed(() => {
    const strategyKey = this.ticket().strategyKey;
    return this.currentView()?.strategies.find(
      (strategy) => strategy.strategy_key === strategyKey,
    ) ?? null;
  });

  protected readonly paramsSchema = computed<DeployStrategyParamsSchema>(
    () => this.selectedStrategy()?.params_schema ?? {},
  );

  /**
   * Which single broker-contacting mode this account's view offers: Paper on
   * a paper account, Shadow on a live one held by the Shadow Account
   * Authority, Live on one custodied by its live authority (ADR 0059 D2/D11).
   * Never two — the view's own `execution_modes` is the sole authority, and
   * there is no `'paper'` fallback once a live-world card is offered.
   */
  protected readonly brokerMode = computed<'paper' | 'shadow' | 'live'>(() => {
    const offered = (mode: 'shadow' | 'live') =>
      this.currentView()?.execution_modes.some(
        (candidate) => candidate.mode === mode && candidate.availability === 'available',
      ) ?? false;
    if (offered('live')) return 'live';
    if (offered('shadow')) return 'shadow';
    return 'paper';
  });

  /** The account's one broker world, as the permission copy words it. */
  protected readonly brokerModeLabel = computed(() => WORLD_LABELS[this.brokerMode()]);

  /**
   * True when the ticket's mode contacts the broker. Dry Run is the only
   * mode that holds no custody, so Paper, Shadow and Live share every gate
   * the backend applies to a broker deploy (`_require_broker_deploy_request`),
   * the evidence-only override included. False until a world is chosen.
   */
  protected readonly brokerModeSelected = computed(() => {
    const mode = this.ticket().executionMode;
    return mode !== null && mode !== 'dry_run';
  });

  /** A Golden review authorizes one exact broker configuration, not a strategy family. */
  protected readonly goldenScopeMatchesTicket = computed(() => {
    const strategy = this.selectedStrategy();
    if (strategy === null || !strategy.golden_validation_scope) return true;
    const ticket = this.ticket();
    return ticket.symbol.trim().toUpperCase() === strategy.validation_case_symbol
      && sameParameterValues(ticket.parameters, strategy.validation_case_parameters);
  });

  /** Other settings are typed only where they may trade: in Dry Run, or for
   * a strategy whose review is not scoped to one Golden configuration. */
  protected readonly parametersEditable = computed(() => {
    const strategy = this.selectedStrategy();
    return strategy === null || !strategy.golden_validation_scope || this.ticket().executionMode === 'dry_run';
  });

  // A broker deploy of an evidence-only strategy carries the durable human
  // override (acknowledgement + reason) on the request itself — restored by
  // operator decision 2026-08-24 after #1702/#1746 re-pointed it at Live.
  // The backend refuses an evidence-only Paper, Shadow *or Live* deploy
  // without it, and rejects one submitted for a fully accepted strategy.
  protected readonly overrideRequired = computed(() =>
    this.selectedStrategy()?.evidence_status === 'evidence_only'
      && this.brokerModeSelected(),
  );

  protected readonly overrideReasonError = computed(() => {
    if (!this.overrideRequired() || !this.overrideReasonTouched()) return null;
    return this.ticket().overrideReason.trim().length >= OVERRIDE_REASON_MIN_LENGTH
      ? null
      : 'Give at least 10 characters explaining why this risk is accepted.';
  });

  // Backend-authored reason this account's own broker option — Paper, Shadow
  // or Live (slice 7) — is unreachable for the selected strategy (#1702).
  // `null` whenever this account's broker mode is admissible or no strategy
  // is selected yet — every non-admissible row is a blocked row today, so
  // `blocked_explanation` is always present here.
  protected readonly brokerModeUnavailableReason = computed(() => {
    const strategy = this.selectedStrategy();
    if (strategy === null || strategy.admissible_modes.includes(this.brokerMode())) return null;
    return strategy.blocked_explanation ?? null;
  });

  // Backend-authored reason the Dry Run option is unreachable (#1703). Only
  // a validated strategy with no registered runtime is ever Dry-Run-blocked
  // — every other row (accepted, evidence-only, or blocked on a stale
  // proof) stays Dry-Run-admissible regardless of validation state.
  protected readonly dryRunUnavailableReason = computed(() => {
    const strategy = this.selectedStrategy();
    if (strategy === null || strategy.admissible_modes.includes('dry_run')) return null;
    return strategy.blocked_explanation ?? null;
  });

  protected readonly selectedExecutionMode = computed(() => {
    const view = this.currentView();
    return view?.execution_modes.find(
      (mode) => mode.mode === this.ticket().executionMode,
    ) ?? null;
  });

  protected readonly quantityLabel = computed(() => {
    const quantity = this.ticket().sizingPreset === 'safe_canary' ? 1 : this.ticket().quantity;
    return `${quantity} ${quantity === 1 ? 'share' : 'shares'}`;
  });

  protected readonly tradesSummary = computed(() => `${this.ticket().symbol || 'No symbol yet'} · ${this.quantityLabel()}`);

  protected readonly exitsSummary = computed(() => {
    const terms = this.exitTerms();
    return terms === null
      ? 'Exit terms not set'
      : `Exit allowance ${terms.exit_allowance_bps} bps, band ${terms.band_multiple}×, spread cap ${terms.spread_cap_bps} bps · fixed for this bot’s life`;
  });

  // ── Steps ─────────────────────────────────────────────────────────────────

  /**
   * The strategy is not yet allowed on this account's broker world, and the
   * owner has not chosen Dry Run, which needs no permission. Its "Allow on
   * {world}" line is step 1's (D9), so What stays open while it waits.
   */
  private readonly permissionPending = computed(() =>
    this.selectedStrategy()?.paper_access_state === 'available' && this.ticket().executionMode !== 'dry_run',
  );

  /** What: a strategy, a valid symbol, parseable settings, and the
   * permission its world needs. */
  protected readonly whatComplete = computed(() =>
    this.selectedStrategy() !== null
      && !this.ticketForm.symbol().invalid()
      && this.invalidParameterFields().size === 0
      && !this.permissionPending(),
  );

  protected readonly whatFoldHint = computed(() => this.permissionPending()
    ? `Allow it on ${this.brokerModeLabel()} first, or choose Dry Run in How.`
    : 'Choose a strategy and a valid symbol first.');

  /** How: an offered world this strategy admits, a size and exit terms. */
  protected readonly howComplete = computed(() => {
    const mode = this.ticket().executionMode;
    const strategy = this.selectedStrategy();
    return mode !== null
      && strategy !== null
      && strategy.admissible_modes.includes(mode)
      && this.selectedExecutionMode()?.availability === 'available'
      && !(this.ticket().sizingPreset === 'custom' && this.ticketForm.quantity().invalid())
      && this.exitTerms() !== null;
  });

  protected readonly whatOpen = computed(() => this.editing().what || !this.whatComplete());
  protected readonly howOpen = computed(() => this.editing().how || !this.howComplete());

  protected readonly permissionSummary = computed(() => {
    const state = this.selectedStrategy()?.paper_access_state;
    const world = this.brokerModeLabel();
    if (state === 'enabled') return `Allowed on this ${world} account`;
    if (state === 'available') return `Not yet allowed on ${world}`;
    return null;
  });

  protected readonly whatSummary = computed(() => {
    const strategy = this.selectedStrategy();
    if (strategy === null) return '';
    const permission = this.permissionSummary();
    const trade = `${strategy.label} on ${this.ticket().symbol}`;
    return permission === null ? trade : `${trade} · ${permission}`;
  });

  protected readonly howSummary = computed(() => {
    const mode = this.selectedExecutionMode()?.label ?? 'Where it trades is not chosen';
    return `${mode} · ${this.quantityLabel()} · ${this.exitsSummary()}`;
  });

  // ── Deploy again ──────────────────────────────────────────────────────────

  protected readonly deployAgainSid = computed(() => {
    const sid = this.queryParams().get(DEPLOY_AGAIN_QUERY_PARAM);
    return sid !== null && INSTANCE_ID_RE.test(sid) ? sid : null;
  });

  /** Deploy again: the earlier bot's sealed settings, never its money or consent. */
  protected readonly prefill = resource({
    params: () => {
      const sid = this.deployAgainSid();
      return sid === null ? undefined : { sid, accountId: this.accountId().trim() };
    },
    loader: ({ params }) => this.panelService.getDeployPrefill(this.deployTarget(params.accountId), params.sid),
  });
  protected readonly prefillError = computed(() => {
    const error = this.prefill.error();
    return error === undefined
      ? null
      : extractServerMessage(error, 'Nothing was pre-filled; the earlier bot’s settings could not be read.');
  });
  /** The earlier bot's strategy is no longer one this account offers. */
  protected readonly prefillStrategyMissing = computed(() => {
    const view = this.currentView();
    if (view === null || this.replaces() === null || !this.prefill.hasValue()) return false;
    const key = this.prefill.value().strategy_key;
    return !view.strategies.some((strategy) => strategy.strategy_key === key);
  });

  // ── Submission ────────────────────────────────────────────────────────────

  protected readonly submissionReadiness = computed<DeploySubmissionReadiness>(() => {
    const view = this.currentView();
    if (!view) {
      return { canSubmit: false, guidance: 'Loading this account’s Deploy checks…' };
    }
    if (!sameAlpacaAccount(view.account_id, this.accountId()) || !sameAlpacaAccount(this.target().accountId ?? '', this.accountId())) {
      return { canSubmit: false, guidance: 'Refreshing the selected account before deployment.' };
    }
    if (this.laneUnenforceable()) {
      return { canSubmit: false, guidance: LANE_FENCE_UNENFORCEABLE_MESSAGE };
    }
    if (this.laneConflict() || this.laneReviewRequired()) {
      return {
        canSubmit: false,
        guidance: 'The account changed. Nothing was sent. Review the refreshed account before deploying.',
      };
    }
    const selectedStrategy = this.selectedStrategy();
    if (selectedStrategy === null) {
      return { canSubmit: false, guidance: 'Choose a strategy in What.' };
    }
    const mode = this.ticket().executionMode;
    if (mode === null) {
      return {
        canSubmit: false,
        guidance: this.permissionPending()
          ? `Allow this strategy on ${this.brokerModeLabel()} in What, or choose Dry Run in How.`
          : 'Choose where this bot trades in How.',
      };
    }
    if (this.exitTerms() === null) {
      return { canSubmit: false, guidance: 'Set this bot’s exit allowance, band multiple and spread cap in How.' };
    }
    if (this.admissionIsStale()) {
      return { canSubmit: false, guidance: 'Refresh the Deploy checks before deploying.' };
    }
    const eligibility = this.brokerModeSelected() ? view.eligibility : view.dry_run_eligibility;
    if (!eligibility.eligible) {
      return {
        canSubmit: false,
        guidance: eligibility.next_action || 'Resolve the failing check before deploying.',
      };
    }
    if (this.submitting()) {
      return { canSubmit: false, guidance: 'Deployment is in progress.' };
    }
    if (this.checkingStatus()) {
      return { canSubmit: false, guidance: 'Checking the last Deploy’s status…' };
    }
    if (this.admissionDecision()?.allowed === false) {
      return { canSubmit: false, guidance: this.admissionDecision()?.next_step ?? this.submitError()?.message ?? 'Refresh the Deploy checks before deploying.' };
    }
    // Mode-aware, not strategy-wide (#1702): a blocked strategy is still
    // Dry-Run-admissible, so admissibility is checked against the ticket's
    // chosen mode, not `selectable` — which means admissible in this
    // account's one broker world, Paper or Shadow.
    if (!selectedStrategy.admissible_modes.includes(mode)) {
      const reason = mode === 'dry_run'
        ? this.dryRunUnavailableReason()
        : this.brokerModeUnavailableReason();
      return {
        canSubmit: false,
        guidance: reason ?? 'This strategy cannot trade in the selected world.',
      };
    }
    if (this.selectedExecutionMode()?.availability !== 'available') {
      return { canSubmit: false, guidance: 'Choose an available world in How.' };
    }
    if (this.ticketForm.symbol().invalid()) {
      return { canSubmit: false, guidance: 'Fix the trading symbol in What.' };
    }
    if (this.brokerModeSelected() && !this.goldenScopeMatchesTicket()) {
      return {
        canSubmit: false,
        guidance: `${this.brokerModeLabel()} trades only this strategy’s qualified symbol and settings. `
          + 'Use the qualified settings in What, or try others in Dry Run.',
      };
    }
    if (this.ticket().sizingPreset === 'custom' && this.ticketForm.quantity().invalid()) {
      return { canSubmit: false, guidance: 'Fix the position size in How.' };
    }
    if (this.invalidParameterFields().size > 0) {
      return { canSubmit: false, guidance: 'Fix the highlighted strategy setting in What.' };
    }
    if (this.overrideRequired()) {
      if (!this.ticket().overrideAcknowledged) {
        return {
          canSubmit: false,
          guidance: 'Acknowledge the evidence-only deployment risk before deploying.',
        };
      }
      if (this.ticket().overrideReason.trim().length < OVERRIDE_REASON_MIN_LENGTH) {
        return {
          canSubmit: false,
          guidance: 'Record why this evidence-only strategy is being deployed.',
        };
      }
    }
    if (!this.ticketForm().valid()) {
      return { canSubmit: false, guidance: 'Complete the highlighted Deploy fields.' };
    }
    return { canSubmit: true, guidance: 'Ready to deploy this bot.' };
  });

  /** Checks that fail for the chosen world, each with its fix — shown only
   * when one fails. Dry Run makes no broker contact, so its one verdict is
   * the Dry Run eligibility, never the broker checks. */
  protected readonly blockers = computed<readonly DeployBlocker[]>(() => {
    const view = this.currentView();
    const mode = this.ticket().executionMode;
    if (view === null || mode === null) return [];
    if (mode === 'dry_run') {
      const eligibility = view.dry_run_eligibility;
      return eligibility.eligible
        ? []
        : [{ id: eligibility.reason_code, label: eligibility.headline, headline: eligibility.explanation, fix: eligibility.next_action }];
    }
    return view.readiness_checks
      .filter((check) => !check.ready)
      .map((check) => ({ id: check.gate_id, label: check.label, headline: check.headline, fix: check.recovery }));
  });

  protected readonly budgetTarget = computed(() => this.deployTarget(this.accountId().trim()), {
    equal: (left, right) => canonicalJson(left) === canonicalJson(right),
  });
  /** The settings the Money step previews. Compared by content, so an edit
   * that changes nothing a preview judges (an override reason) does not
   * re-preview the money. */
  protected readonly budgetBody = computed<DeployBotBody | null>(() => {
    const strategy = this.selectedStrategy();
    const mode = this.ticket().executionMode;
    if (strategy === null || mode === null || this.exitTerms() === null
      || this.ticketForm.symbol().invalid() || this.ticketForm.quantity().invalid() || this.invalidParameterFields().size > 0) return null;
    return this.deployBody(this.ticket(), strategy, mode);
  }, { equal: (left, right) => canonicalJson(left) === canonicalJson(right) });

  /** The Money step's review, while it still describes these exact settings. */
  protected readonly currentMoneyReview = computed(() => {
    const review = this.moneyReview();
    const body = this.budgetBody();
    return review !== null && body !== null && review.context === budgetReviewContext(this.budgetTarget(), body)
      ? review : null;
  });
  private readonly reviewToken = computed(() => this.currentMoneyReview()?.preview.review_token ?? null);

  private readonly validBudget = computed<DeploymentBudgetInput | null>(() => {
    const review = this.currentMoneyReview();
    if (review === null || !review.preview.review_token) return null;
    const phrase = review.preview.confirmation_text ?? null;
    if (phrase !== null && this.liveConsent() !== phrase) return null;
    return {
      amount_usd: review.amount,
      risk_revision: review.preview.risk_revision ?? 0,
      review_token: review.preview.review_token,
      live_confirmation: phrase === null ? null : this.liveConsent(),
    };
  });

  protected readonly canSubmit = computed(() => this.submissionReadiness().canSubmit && this.validBudget() !== null);
  protected readonly submitGuidance = computed(() => {
    const readiness = this.submissionReadiness();
    if (!readiness.canSubmit) return readiness.guidance;
    const review = this.currentMoneyReview();
    if (review === null) return 'Choose a dollar budget in Money. It is previewed before you can deploy.';
    const phrase = review.preview.confirmation_text ?? null;
    if (phrase !== null && this.liveConsent() !== phrase) return 'Type the phrase above exactly to confirm this real-money Deploy.';
    return readiness.guidance;
  });

  protected readonly hasFrozenCommand = computed(() => this.frozenCommand() !== null);

  // ── Recovery and receipt ──────────────────────────────────────────────────

  /** A submission this page does not hold — a reload, or a return by the
   * browser's history — whose recorded outcome is read, never re-sent. The
   * page's own Deploy is read only while its receipt is still pending; one
   * whose answer was lost is read when the owner asks (`checkSubmission`). */
  protected readonly recoveryKey = computed(() => {
    const key = this.queryParams().get(SUBMISSION_PARAM);
    if (key === null || !SUBMISSION_KEY_RE.test(key) || this.restoredDraftKey() !== this.draftKey()) return null;
    const own = key === this.submissionKey() || key === this.writtenKey();
    return !own || this.receipt()?.status === 'pending' ? key : null;
  });
  protected readonly recoveredCommand = resource({
    params: () => {
      const key = this.recoveryKey();
      return key === null ? undefined : { key, accountId: this.accountId().trim() };
    },
    loader: ({ params }) => this.panelService.getDeploySubmission(this.deployTarget(params.accountId), params.key),
  });
  /** The recovery read found no committed Deploy for the key. */
  protected readonly recoveryNotCommitted = computed(() => {
    const error = this.recoveredCommand.error();
    return error instanceof HttpErrorResponse && error.status === 404
      ? extractServerMessage(error, NOT_COMMITTED_MESSAGE)
      : null;
  });
  /** The recovery read found the key claimed but not committed. */
  protected readonly recoveryClaim = computed(() =>
    this.recoveredCommand.hasValue() ? uncommittedClaim(this.recoveredCommand.value()) : null,
  );

  /** What Confirm says went wrong: the last answer, or — back on a form
   * whose Deploy got none — that its outcome is still unknown. */
  protected readonly confirmError = computed(() =>
    this.submitError() ?? (this.outcomeUnknown() ? UNKNOWN_OUTCOME : null),
  );
  protected readonly shownReceipt = computed(() => {
    const own = this.receipt();
    const recovered = this.recoveredCommand.hasValue() ? committedReceipt(this.recoveredCommand.value()) : null;
    const receipt = own?.status === 'pending' && recovered?.command_id === own.command_id ? recovered : (own ?? recovered);
    return receipt && sameAlpacaAccount(receipt.account_id, this.accountId()) ? receipt : null;
  });
  protected readonly receiptBotLink = computed<AccountWorkspaceLink | null>(() => {
    const receipt = this.shownReceipt();
    if (receipt === null) return null;
    const target = this.target();
    return accountWorkspaceBotRoute(
      { broker: target.broker, clerkId: target.clerkId, accountId: this.accountId() },
      receipt.strategy_instance_id,
      'bots',
    );
  });

  /** Where this account's defaults for new bots are set (H3). */
  protected readonly settingsRoute = computed(() => {
    const target = this.target();
    return accountWorkspaceTabRoute({ broker: target.broker, clerkId: target.clerkId, accountId: this.accountId() }, 'settings');
  });

  protected async newDeployment(): Promise<void> {
    // The recovery hint leaves the URL first, so the form returns without a
    // recovery read flashing in between.
    await this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { [SUBMISSION_PARAM]: null, [DEPLOY_AGAIN_QUERY_PARAM]: null },
      queryParamsHandling: 'merge',
      replaceUrl: true,
    });
    this.receipt.set(null);
    this.frozenCommand.set(null);
    this.moneyReview.set(null);
    this.liveConsent.set('');
  }

  constructor() {
    this.destroyRef.onDestroy(() => {
      if (this.symbolScopeTimer !== null) clearTimeout(this.symbolScopeTimer);
    });

    // The draft: restored when the page opens on an account, and kept on
    // every change after (H9). Created first so a restore lands before any
    // other effect reads the form.
    effect(() => {
      const key = this.draftKey();
      const draft: DeployDraft = {
        settings: settingsOf(this.ticket()),
        amount: this.amount(),
        submissionKey: this.submissionKey(),
        submittedContent: this.submittedContent(),
        outcomeUnknown: this.outcomeUnknown(),
        editing: this.editing(),
        replaces: this.replaces(),
      };
      if (key !== untracked(this.restoredDraftKey)) {
        this.restoredDraftKey.set(key);
        untracked(() => this.restoreDraft(this.drafts.read(key) ?? freshDeployDraft()));
        return;
      }
      this.drafts.write(key, draft);
    });

    effect(() => {
      const view = this.currentView();
      if (view && !this.termsSeeded) {
        const terms = view.default_exit_terms;
        // Pre-filled from this account's defaults for new bots (H3). A view
        // with no defaults leaves the terms to the owner, and the next view
        // that has them may still seed.
        if (terms) {
          this.termsSeeded = true;
          this.ticket.update(ticket => ({ ...ticket, exitAllowanceBps: terms.exit_allowance_bps,
            bandMultiple: terms.band_multiple, spreadCapBps: terms.spread_cap_bps }));
        }
      }
      const current = untracked(this.ticket);
      const requestedKey = this.queryParams().get('strategy') ?? this.queryParams().get('strategy_key');
      // Route reuse keeps this component alive while its query parameters
      // change. A newly supplied strategy deep link is explicit owner intent
      // and must outrank the ticket's prior selection once; later
      // symbol-scoped catalog refreshes keep the current ticket strategy
      // ahead of the unchanged link.
      const requestedSelectionChanged = requestedKey !== null && requestedKey !== this.lastRequestedStrategyKey;
      const strategy = (requestedSelectionChanged
        ? view?.strategies.find((candidate) => candidate.strategy_key === requestedKey)
        : view?.strategies.find((candidate) => candidate.strategy_key === current.strategyKey))
        ?? view?.strategies.find((candidate) => candidate.strategy_key === requestedKey)
        ?? view?.strategies.find((candidate) => candidate.selectable)
        ?? view?.strategies[0];
      if (!strategy) return;
      this.lastRequestedStrategyKey = requestedKey;
      // A scoped catalog can legitimately omit the ticket's prior strategy
      // (for example, its Golden evidence only applies to another symbol).
      // Once selection falls through to the request/default candidate, carry
      // that resolved key into the ticket; retaining the vanished key leaves
      // `selectedStrategy` null against the refreshed view.
      const strategyKey = strategy.strategy_key;
      const nextValidationScope = validationScopeSeed(strategy);
      const scopeChanged = !sameValidationScope(this.lastValidationScope, nextValidationScope);
      const priorParametersRemainIntact = this.lastValidationScope !== null
        && sameParameterValues(current.parameters, this.lastValidationScope.parameters);
      const strategyChanged = strategyKey !== current.strategyKey;
      if (strategyChanged) {
        // Route-driven selection is semantically the same strategy switch as
        // the strategy control: a prior strategy's admission result and
        // evidence-only acknowledgement must never carry into the new one.
        this.clearAdmission();
        this.overrideReasonTouched.set(false);
        this.ticket.update((ticket) => ({
          ...ticket,
          strategyKey,
          parameters: { ...strategy.validation_case_parameters },
          overrideAcknowledged: false,
          overrideReason: '',
        }));
      } else if (scopeChanged && priorParametersRemainIntact) {
        this.ticket.update((ticket) => ({
          ...ticket,
          parameters: { ...strategy.validation_case_parameters },
        }));
      }
      this.lastValidationScope = nextValidationScope;
      const symbol = current.symbol || strategy.validation_case_symbol;
      if (symbol !== current.symbol) this.applySymbol(symbol);
    });

    // No world is chosen for the owner (H17) — except this lane's own Paper
    // or Shadow, which never spends real money. Live is always the owner's
    // own click.
    effect(() => {
      const view = this.currentView();
      const strategy = this.selectedStrategy();
      const lane = this.brokerMode();
      if (view === null || strategy === null || lane === 'live') return;
      const offered = view.execution_modes.some((mode) => mode.mode === lane && mode.availability === 'available');
      if (!offered || !strategy.admissible_modes.includes(lane)) return;
      untracked(() => this.ticket.update((ticket) =>
        ticket.executionMode === null ? { ...ticket, executionMode: lane } : ticket,
      ));
    });

    // A step the owner is working in stays open until they press Done: a
    // step that folded the moment it became complete would take the control
    // they were using away from under the keyboard.
    effect(() => {
      if (this.currentView() === null || this.selectedStrategy() === null) return;
      const what = !this.whatComplete();
      const how = !this.howComplete();
      untracked(() => this.editing.update((editing) =>
        (what && !editing.what) || (how && !editing.how)
          ? { what: editing.what || what, how: editing.how || how }
          : editing,
      ));
    });

    // Deploy again pre-fills once per earlier bot; a draft that already came
    // from it keeps the owner's edits.
    effect(() => {
      if (!this.prefill.hasValue()) return;
      const prefill = this.prefill.value();
      untracked(() => {
        if (this.replaces() !== prefill.source_strategy_instance_id) this.applyPrefill(prefill);
      });
    });

    // Live's typed phrase proves one review: a new amount, new settings or a
    // new review clears it (PRD #2560 story 60).
    effect(() => {
      this.reviewToken();
      untracked(() => this.liveConsent.set(''));
    });

    effect(() => this.syncFrozenCommandDrift());
    effect(() => {
      this.fence();
      untracked(() => {
        this.frozenCommand.set(null);
        this.admissionDecision.set(null);
        this.laneConflict.set(false);
        this.submitError.set(null);
      });
    });
  }

  private restoreDraft(draft: DeployDraft): void {
    this.ticket.set(ticketOf(draft.settings));
    this.amount.set(draft.amount);
    this.submissionKey.set(draft.submissionKey);
    this.submittedContent.set(draft.submittedContent);
    this.outcomeUnknown.set(draft.outcomeUnknown);
    this.editing.set(draft.editing);
    this.replaces.set(draft.replaces);
    this.moneyReview.set(null);
    this.liveConsent.set('');
    this.overrideReasonTouched.set(false);
    this.termsSeeded = draft.settings.exitAllowanceBps !== null;
    this.lastValidationScope = null;
    if (draft.settings.symbol) this.scheduleSymbolScope(draft.settings.symbol);
  }

  /** A fresh form. A Deploy whose outcome is not known yet keeps its key
   * through it: the key goes only when its status read settles it. */
  private freshDraft(settings: DeployTicketSettings, replaces: string | null = null): DeployDraft {
    const fresh = { ...freshDeployDraft(settings), replaces };
    return this.outcomeUnknown()
      ? { ...fresh, submissionKey: this.submissionKey(), submittedContent: this.submittedContent(), outcomeUnknown: true }
      : fresh;
  }

  /** Deploy again: a fresh draft from the earlier bot's sealed settings. Its
   * money and consent are never copied, and it gets its own submission key. */
  private applyPrefill(prefill: BotDeployPrefill): void {
    const current = this.ticket();
    const terms = prefill.exit_terms ?? null;
    this.receipt.set(null);
    this.frozenCommand.set(null);
    this.clearAdmission();
    this.submitError.set(null);
    this.restoreDraft(this.freshDraft(
      {
        ...EMPTY_DEPLOY_SETTINGS,
        strategyKey: prefill.strategy_key,
        symbol: prefill.symbol.trim().toUpperCase(),
        sizingPreset: prefill.sizing.preset ?? 'safe_canary',
        quantity: prefill.sizing.quantity ?? 1,
        parameters: { ...prefill.parameters },
        // Terms the earlier bot never recorded in full start from this
        // account's defaults for new bots.
        exitAllowanceBps: terms?.exit_allowance_bps ?? current.exitAllowanceBps,
        bandMultiple: terms?.band_multiple ?? current.bandMultiple,
        spreadCapBps: terms?.spread_cap_bps ?? current.spreadCapBps,
      },
      prefill.source_strategy_instance_id,
    ));
  }

  /** Clear: back to a fresh form on this account's default strategy, and
   * the earlier bot is no longer named. */
  protected async clearPrefill(): Promise<void> {
    const current = this.ticket();
    this.restoreDraft(this.freshDraft({
      ...EMPTY_DEPLOY_SETTINGS,
      exitAllowanceBps: current.exitAllowanceBps,
      bandMultiple: current.bandMultiple,
      spreadCapBps: current.spreadCapBps,
    }));
    const view = this.currentView();
    const strategy = view?.strategies.find((candidate) => candidate.selectable) ?? view?.strategies[0];
    if (strategy) this.setStrategyKey(strategy.strategy_key);
    await this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { [DEPLOY_AGAIN_QUERY_PARAM]: null },
      queryParamsHandling: 'merge',
      replaceUrl: true,
    });
  }

  /** A changed account abandons the attempt and refreshes the page context. */
  private syncFrozenCommandDrift(): void {
    const frozen = this.frozenCommand();
    if (frozen === null) return;
    const routeKey = this.commandRouteKey(this.target());
    if (frozen.routeKey !== routeKey || !sameAlpacaAccount(frozen.target.accountId ?? '', this.accountId())) {
      this.frozenCommand.set(null);
      this.laneConflict.set(true);
      void this.refreshAfterRebind();
      this.submitError.set({
        outcome: 'conflict',
        title: this.errorTitle('conflict'),
        message: 'The account changed. Nothing was sent; the page refreshed. Review the current account before deploying.',
        explanation: null,
        nextAction: null,
        receiptId: null,
        recordedAtMs: null,
      });
    } else if (frozen.ticketKey !== this.ticketKey(this.ticket())) {
      this.frozenCommand.set(null);
    }
  }

  private async refreshAfterRebind(): Promise<void> {
    try {
      await this.fleetDirectory.refresh();
      this.frozenCommand.set(null);
      this.admissionDecision.set(null);
      this.deployView.reload();
    } catch {
      this.submitError.set({ outcome: 'blocked', title: 'Account refresh failed',
        message: 'Nothing was sent. Refresh the account before trying again.', explanation: null,
        nextAction: 'Refresh the page.', receiptId: null, recordedAtMs: null });
    }
  }

  protected editStep(step: keyof DeployStepEditing): void {
    this.editing.update((editing) => ({ ...editing, [step]: true }));
  }

  protected foldStep(step: keyof DeployStepEditing): void {
    this.editing.update((editing) => ({ ...editing, [step]: false }));
  }

  protected setStrategyKey(value: DeployBotStrategy['strategy_key']): void {
    const strategy = this.currentView()?.strategies.find((candidate) => candidate.strategy_key === value);
    if (!strategy) return;
    this.clearAdmission();
    const current = this.ticket();
    const previous = this.currentView()?.strategies.find(
      (candidate) => candidate.strategy_key === current.strategyKey,
    );
    // An owner's own symbol survives a strategy switch; a symbol that was
    // only the previous strategy's validation case is replaced by the new
    // strategy's.
    const symbol = current.symbol && current.symbol !== previous?.validation_case_symbol
      ? current.symbol
      : strategy.validation_case_symbol;
    this.ticket.update((ticket) => ({
      ...ticket,
      strategyKey: strategy.strategy_key,
      parameters: { ...strategy.validation_case_parameters },
      overrideAcknowledged: false,
      overrideReason: '',
    }));
    this.lastValidationScope = validationScopeSeed(strategy);
    this.applySymbol(symbol);
    this.overrideReasonTouched.set(false);
  }

  protected setOverrideAcknowledged(checked: boolean): void {
    this.clearAdmission();
    this.ticket.update((current) => ({ ...current, overrideAcknowledged: checked }));
  }

  protected setOverrideReason(value: string): void {
    this.clearAdmission();
    this.ticket.update((current) => ({ ...current, overrideReason: value }));
  }

  protected touchOverrideReason(): void {
    this.overrideReasonTouched.set(true);
  }

  protected setParameter(change: { field: string; value: string | number }): void {
    this.clearAdmission();
    this.ticket.update((current) => ({
      ...current,
      parameters: { ...current.parameters, [change.field]: change.value },
    }));
  }

  protected useQualifiedConfiguration(configuration: QualifiedDeployConfiguration): void {
    // The shared instrument card already proved this pick's lake coverage.
    // Apply the exact server tuple together; never merge it with old overrides.
    const current = this.ticket();
    if (current.symbol === configuration.symbol && sameParameterValues(current.parameters, configuration.parameters)
      && this.invalidParameterFields().size === 0) return;
    this.clearAdmission();
    this.invalidParameterFields.set(new Set());
    this.ticket.update(ticket => ({ ...ticket, symbol: configuration.symbol, parameters: { ...configuration.parameters } }));
    this.scheduleSymbolScope(configuration.symbol);
  }

  protected setInvalidParameterFields(fields: ReadonlySet<string>): void {
    this.invalidParameterFields.set(fields);
  }

  protected setSymbol(value: string): void {
    this.clearAdmission();
    this.applySymbol(value.trim().toUpperCase());
  }

  /**
   * The single writer of the ticket symbol, and therefore the single place
   * readiness is re-scoped. All three paths that move the symbol go through
   * here: the view's seeding effect, a strategy switch, and the owner's own
   * pick.
   *
   * Two of the three used to write the ticket directly. That left the page
   * showing ACCOUNT-level channel health under a populated symbol on first
   * paint, and, after a strategy switch, symbol-A's checks under symbol B,
   * with `canSubmit` gating on them.
   *
   * The loop this used to be feared for stays closed downstream, in
   * `scheduleSymbolScope`: it stops as soon as the checks on screen already
   * describe the symbol being applied, which is what the returning view
   * re-seeds.
   */
  private applySymbol(symbol: string): void {
    this.ticket.update((current) =>
      current.symbol === symbol ? current : { ...current, symbol },
    );
    this.scheduleSymbolScope(symbol);
  }

  private scheduleSymbolScope(symbol: string): void {
    if (this.symbolScopeTimer !== null) clearTimeout(this.symbolScopeTimer);
    this.symbolScopeTimer = setTimeout(() => {
      this.symbolScopeTimer = null;
      // A half-typed ticker is not a scope. Wait for a symbol the broker
      // contract would actually accept rather than round-tripping a 422.
      if (!SYMBOL_RE.test(symbol)) return;
      // The checks that actually loaded already describe this symbol — the
      // condition that closes the seed loop, and the one that lets a failed
      // scope be retried (the requested scope alone cannot tell them apart).
      if (this.lastLoadedView()?.symbol === symbol) return;
      this.scopedSymbol.set(symbol);
      // A resource mid-load refuses `reload()`. Re-arm instead of dropping
      // the newer scope on the floor: dropped, it strands the page on checks
      // for a symbol the owner has already left, with no way back.
      if (!this.deployView.reload()) this.scheduleSymbolScope(symbol);
    }, SYMBOL_SCOPE_DEBOUNCE_MS);
  }

  protected setSizingPreset(value: DeploySizingPreset): void {
    this.clearAdmission();
    this.ticket.update((current) => ({
      ...current,
      sizingPreset: value,
      quantity: value === 'safe_canary' ? 1 : current.quantity,
    }));
  }

  protected setQuantity(quantity: number): void {
    this.clearAdmission();
    this.ticket.update((current) => ({ ...current, quantity }));
  }

  protected setExecutionMode(mode: DeployExecutionMode['mode']): void {
    const option = this.currentView()?.execution_modes.find(
      (candidate) => candidate.mode === mode,
    );
    if (option?.availability !== 'available') return;
    if (mode !== 'dry_run' && this.brokerModeUnavailableReason() !== null) return;
    if (mode === 'dry_run' && this.dryRunUnavailableReason() !== null) return;
    this.clearAdmission();
    this.ticket.update((current) => ({
      ...current,
      executionMode: mode,
      allowCarryover: mode === 'dry_run' ? false : current.allowCarryover,
    }));
  }

  protected setCarryover(checked: boolean): void {
    if (this.ticket().executionMode === 'dry_run') return;
    if (!this.currentView()?.carryover_available) return;
    this.clearAdmission();
    this.ticket.update((current) => ({ ...current, allowCarryover: checked }));
  }

  protected touchQuantity(): void {
    this.ticketForm.quantity().markAsTouched();
  }

  protected reload(): void {
    this.submitError.set(null);
    this.admissionDecision.set(null);
    this.deployView.reload();
  }

  protected paperAccessChanged(): void {
    this.clearAdmission();
    this.deployView.reload();
  }

  protected clearAdmission(): void {
    this.admissionDecision.set(null);
  }

  protected async submit(): Promise<void> {
    this.markFormTouched();
    const view = this.currentView();
    const strategy = this.selectedStrategy();
    const mode = this.ticket().executionMode;
    const budget = this.validBudget();
    if (!view || !strategy || mode === null || budget === null || !this.canSubmit()) return;

    this.submitting.set(true);
    this.submitError.set(null);
    this.admissionDecision.set(null);
    const settings = { ...this.deployBody(this.ticket(), strategy, mode), budget };
    const submission = this.submissionFor(settings, view.account_id);
    const commandTarget = this.commandTargetFor(submission, view.account_id);
    let refused = false;
    let sent = false;

    try {
      // The Start plan judges the settings alone: no submission key, no lineage.
      const decision = await this.panelService.previewStartAdmission(commandTarget, settings);
      if (!this.submissionStillCurrent(settings)) return;
      this.admissionDecision.set(decision);
      if (!decision.allowed) {
        refused = true;
        return;
      }
      // A read-only recovery hint survives a reload; the server's recorded
      // outcome for this key stays the authority on what was committed.
      await this.writeSubmissionParam(submission.submission_key);
      if (!this.submissionStillCurrent(settings)) return;
      sent = true;
      this.acceptReceipt(await this.panelService.deployBudgetBot(commandTarget, submission));
    } catch (error) {
      refused = true;
      const decision = this.admissionFromError(error);
      if (decision) this.admissionDecision.set(decision);
      const failure = this.toDeployError(error);
      this.submitError.set(failure);
      // An answer that does not settle this key keeps it for the status read;
      // a definite refusal frees the form to mint a new one on an edit.
      if (sent) this.outcomeUnknown.set(failure.outcome === 'unknown');
      if (error instanceof HttpErrorResponse && error.status === 409
        && ['clerk_binding_generation_conflict', 'clerk_routing_epoch_conflict'].includes(error.error?.detail?.reason ?? error.error?.detail?.reason_code)) {
        this.frozenCommand.set(null);
        this.laneConflict.set(true);
        await this.refreshAfterRebind();
      }
    } finally {
      this.submitting.set(false);
      if (refused) this.focusAfterRender(() => this.confirmStep()?.focusRefusal());
    }
  }

  /** A Deploy's receipt, from its own answer or from its status read. */
  private acceptReceipt(receipt: BudgetDeployReceipt): void {
    this.receipt.set(receipt);
    this.frozenCommand.set(null);
    this.submitError.set(null);
    this.outcomeUnknown.set(false);
    // The next Deploy from this form is a new bot: a new key, and fresh
    // money and consent. Its settings stay for a twin.
    this.submissionKey.set(crypto.randomUUID());
    this.submittedContent.set(null);
    this.amount.set('');
    this.replaces.set(null);
    this.focusAfterRender(() => this.receiptPanel()?.focus());
  }

  /** The sent Deploy is known not to have committed, so nothing was set
   * aside: the next Deploy is a new submission under a new key. */
  private releaseSubmission(): void {
    this.outcomeUnknown.set(false);
    this.submissionKey.set(crypto.randomUUID());
    this.submittedContent.set(null);
    this.frozenCommand.set(null);
  }

  /**
   * "Check deployment status" for a Deploy whose outcome is unknown: reads
   * what its key recorded and never sends it again. A receipt shows the bot;
   * `in_flight` or an unreadable answer keeps the key; only `not_committed`
   * or no record at all frees the form for a new key.
   */
  protected async checkSubmission(): Promise<void> {
    const key = this.submissionKey();
    if (!this.outcomeUnknown() || this.checkingStatus()) return;
    this.checkingStatus.set(true);
    try {
      const answer = await this.panelService.getDeploySubmission(this.deployTarget(this.accountId().trim()), key);
      if (key !== this.submissionKey()) return;
      if ('receipt_id' in answer) {
        this.acceptReceipt(answer);
        // Its receipt, like one Deploy answered with, is re-read on a reload.
        await this.writeSubmissionParam(key);
        return;
      }
      this.submitError.set(this.claimError(answer));
      if (answer.status === 'not_committed') this.settleNotCommitted();
    } catch (error) {
      if (key !== this.submissionKey()) return;
      if (error instanceof HttpErrorResponse && error.status === 404) {
        this.submitError.set(deployNotice('blocked', 'Not deployed', extractServerMessage(error, NOT_COMMITTED_MESSAGE)));
        this.settleNotCommitted();
      } else {
        // No runner, or no answer: still unknown, never "not deployed".
        this.submitError.set(deployNotice(
          'unknown',
          'Outcome unknown',
          extractServerMessage(error, 'The deployment result could not be read.'),
          'Whether the bot started is still not known; checking never starts a second bot.',
          'Check deployment status again in a moment.',
        ));
      }
    } finally {
      this.checkingStatus.set(false);
    }
  }

  /** Not committed: the Check button goes, so the keyboard moves to the answer. */
  private settleNotCommitted(): void {
    this.releaseSubmission();
    this.focusAfterRender(() => this.confirmStep()?.focusRefusal());
  }

  /** An uncommitted claim, in the backend's own words. */
  private claimError(claim: DeploySubmissionUncommitted): DeployError {
    return claim.status === 'in_flight'
      ? deployNotice('unknown', 'Still being deployed', claim.message, claim.explanation, claim.next_action)
      : deployNotice('blocked', 'Not deployed', claim.message, claim.explanation, claim.next_action);
  }

  /** Names `key` in the URL — the hint a reload reads — as this page's own. */
  private async writeSubmissionParam(key: string): Promise<void> {
    this.writtenKey.set(key);
    await this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { [SUBMISSION_PARAM]: key },
      queryParamsHandling: 'merge',
      replaceUrl: true,
    });
  }

  private focusAfterRender(focus: () => void): void {
    afterNextRender({ write: focus }, { injector: this.injector });
  }

  private deployBody(
    ticket: AlpacaDeployTicket,
    strategy: DeployBotStrategy,
    mode: NonNullable<AlpacaDeployTicket['executionMode']>,
  ): DeployBotBody {
    const exitTerms = this.exitTerms();
    if (exitTerms === null) throw new Error("Exit terms are required before deployment.");
    const body: DeployBotBody = {
      exit_terms: exitTerms,
      strategy_key: strategy.strategy_key,
      symbol: ticket.symbol.trim().toUpperCase(),
      sizing: {
        preset: ticket.sizingPreset,
        quantity: ticket.sizingPreset === 'safe_canary' ? 1 : ticket.quantity,
      },
      execution_mode: mode,
      carryover_policy: ticket.allowCarryover ? 'ALLOW' : 'FORBID',
      // `parameters` genuinely varies by strategy (unlike `params_schema`,
      // which has one uniform shape typed at the OpenAPI boundary) — the
      // generated type narrows this dict[str, Any] request field to
      // `Record<string, never>`, the same pre-existing codegen limitation
      // noted in strategy-lab-runner.service.ts's own `params` construction.
      parameters: ticket.parameters as unknown as DeployBotBody['parameters'],
    };
    // The durable evidence-only override rides the broker request — Paper,
    // Shadow or Live alike, which hold the same custody (operator decision
    // 2026-08-24, restoring what #1702 re-pointed at Live). Only an
    // evidence-only strategy carries it — the backend rejects an override on
    // an accepted strategy as superfluous.
    if (strategy.evidence_status === 'evidence_only' && mode !== 'dry_run') {
      body.evidence_override = {
        acknowledgement: 'I_ACCEPT_EVIDENCE_ONLY_DEPLOYMENT_RISK',
        reason: ticket.overrideReason.trim(),
      };
    }
    return body;
  }

  /**
   * The Deploy command for these settings, under the submission key the
   * backend dedupes on (#2551).
   *
   * The key is reused only for exactly the settings it was first sent with —
   * the same content the backend's fingerprint hashes (never the review
   * token or typed phrase, which prove the click rather than describe the
   * bot). Other settings under the same key would be refused as a conflict,
   * so they mint a new key: a new bot, by the owner's own changed choice.
   * Not while that Deploy's outcome is unknown: a new key then could start a
   * second bot beside one that committed, so the key stays and the backend
   * answers for it.
   */
  private submissionFor(settings: DeployBotBody & { budget: DeploymentBudgetInput }, accountId: string): DeploySubmission {
    const content = canonicalJson({
      account: accountId,
      settings: { ...settings, budget: { amount_usd: settings.budget.amount_usd, risk_revision: settings.budget.risk_revision } },
    });
    const prior = this.submittedContent();
    if (prior !== null && prior !== content && !this.outcomeUnknown()) this.submissionKey.set(crypto.randomUUID());
    this.submittedContent.set(content);
    const replaces = this.replaces();
    return {
      ...settings,
      submission_key: this.submissionKey(),
      ...(replaces === null ? {} : { replaces_strategy_instance_id: replaces }),
    };
  }

  /**
   * Preview and apply (including an uncertain-outcome retry) are one durable
   * command. A changed ticket or route produces a different context and
   * explicitly abandons the prior key before minting another.
   */
  private commandTargetFor(body: DeploySubmission, accountId: string): ResourceTarget {
    const lane = fencedTarget(this.target(), this.fence());
    const context = JSON.stringify({
      broker: lane.broker,
      clerkId: lane.clerkId,
      accountId,
      bindingGeneration: lane.bindingGeneration,
      routingEpoch: lane.routingEpoch,
      body,
    });
    const frozen = this.frozenCommand();
    if (frozen?.context === context) return frozen.target;

    const target = withCommand(withAccount(lane, accountId), 'bot_action', crypto.randomUUID());
    this.frozenCommand.set({
      context,
      target,
      ticketKey: this.ticketKey(this.ticket()),
      routeKey: this.commandRouteKey(lane),
    });
    return target;
  }

  private ticketKey(ticket: AlpacaDeployTicket): string {
    return JSON.stringify(ticket);
  }

  private commandRouteKey(target: ResourceTarget): string {
    return laneKey(
      target.broker,
      target.clerkId,
      target.routingEpoch,
      target.bindingGeneration,
    );
  }

  /** The settings and reviewed budget on screen are still the ones sent. */
  private submissionStillCurrent(submitted: DeployBotBody & { budget: DeploymentBudgetInput }): boolean {
    const strategy = this.selectedStrategy();
    const mode = this.ticket().executionMode;
    if (!strategy || mode === null) return false;
    return canonicalJson({ ...this.deployBody(this.ticket(), strategy, mode), budget: this.validBudget() })
      === canonicalJson(submitted);
  }

  protected symbolError(): string | null {
    if (!this.ticketForm.symbol().touched()) return null;
    return this.ticketForm.symbol().errors()[0]?.message ?? null;
  }

  protected quantityError(): string | null {
    if (this.ticket().sizingPreset !== 'custom' || !this.ticketForm.quantity().touched()) {
      return null;
    }
    return this.ticketForm.quantity().errors()[0]?.message ?? null;
  }

  private admissionFromError(error: unknown): RunAdmissionDecision | null {
    if (!(error instanceof HttpErrorResponse)) return null;
    const admission = error.error?.detail?.admission as RunAdmissionDecision | undefined;
    return admission ?? null;
  }

  private markFormTouched(): void {
    this.ticketForm.strategyKey().markAsTouched();
    this.ticketForm.symbol().markAsTouched();
    this.ticketForm.quantity().markAsTouched();
  }

  private toDeployError(error: unknown): DeployError {
    if (error instanceof HttpErrorResponse) {
      const detail = error.error?.detail as {
        outcome?: 'conflict' | 'blocked' | 'unknown';
        receipt_id?: string | null;
        recorded_at_ms?: number | null;
        message?: string;
        why?: string | null;
        next_action?: string | null;
        reason_code?: string | null;
      } | undefined;
      if (detail?.message) {
        const outcome = SUBMISSION_UNSETTLED_REASONS.has(detail.reason_code ?? '')
          ? 'unknown'
          : detail.outcome ?? (error.status === 409 ? 'conflict' : 'blocked');
        return {
          outcome,
          title: this.errorTitle(outcome),
          message: detail.message,
          explanation: detail.why ?? null,
          nextAction: detail.next_action ?? null,
          receiptId: detail.receipt_id ?? null,
          recordedAtMs: detail.recorded_at_ms ?? null,
        };
      }
    }
    return UNKNOWN_OUTCOME;
  }

  private errorTitle(outcome: DeployError['outcome']): string {
    if (outcome === 'conflict') return 'The account changed before Deploy';
    if (outcome === 'blocked') return 'Deploy refused';
    return 'Outcome unknown';
  }
}
