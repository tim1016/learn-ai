import { provideZonelessChangeDetection } from "@angular/core";
import { HttpErrorResponse } from "@angular/common/http";
import { render, screen, waitFor } from "@testing-library/angular";
import userEvent from "@testing-library/user-event";
import { of, throwError } from "rxjs";
import { describe, expect, it, vi } from "vitest";

import { GoldenValidationService } from "../../services/golden-validation.service";
import type { GoldenValidation, ReviewGoldenValidationRequest } from "../../services/golden-validation.types";
import { GoldenValidationWorkbenchComponent } from "./golden-validation-workbench.component";

const CASE: GoldenValidation = {
  id: 7,
  source_run_id: 44,
  label: "SPY September baseline",
  strategy_name: "ema_crossover",
  symbol: "SPY",
  rationale: "Selected after parameter exploration.",
  designated_by: "researcher",
  designated_at_ms: Date.UTC(2026, 8, 10),
  state: "candidate",
  validation_case: {
    schema_version: 1,
    source_run_id: 44,
    strategy: { name: "ema_crossover", program_version: "ema-v1" },
    symbol: "SPY",
    parameters: { crossover_gap: 0.2, rsi_min: 50, rsi_max: 70 },
    window: { start_ms: 1767589200000, end_ms: 1767675600000, timespan: "minute" },
    data_policy: {
      source: "polygon",
      fixture_id: "spy-fixture",
      fixture_sha256: "a4f729d1e8c0",
    },
    execution: {
      fill_mode: "signal_bar_close",
      initial_cash: 100000,
      commission_per_order: 1,
      brokerage_policy: "default",
      configuration: {
        compatibility_profile: "us-equity-raw-ibkr-v1",
        warmup_from_date: null,
        slippage_per_share: 0,
        session_entry_cutoff: null,
        force_flat_at: null,
        limit_penetration: 0,
        source_ref: "references/lean/ema-crossover.py",
      },
    },
    requested_engine: "both",
    parity_group_id: "parity-44",
  },
  evidence_state: "deviations",
  evidence_revision: "a".repeat(64),
  parity_evidence: {
    computed_state: "deviations",
    qualification_warnings: [],
    parity_verdict: {
      status: "diverged",
      left_run_id: 44,
      right_run_id: 45,
      payload: {
        schema_version: 3,
        status: "diverged",
        reason: "trade_reconciliation_diverged",
        divergences: [{ category: "DECISION_MISMATCH", trade_number: 2, ms_utc: 1767589200000, message: "trade #2 present only on right side" }],
      },
    },
  },
  latest_review: null,
  review_is_current: null,
  reviews: [],
};

const REVIEW = {
  id: 4,
  decision: "accept" as const,
  classification: "reviewed_deviations" as const,
  evidence_state: "deviations",
  reason: "Reviewed the extra LEAN trade.",
  quantconnect_backtest_id: null,
  authorized_program_version: null,
  reviewed_by: "researcher",
  reviewed_at_ms: Date.UTC(2026, 8, 10),
  expected_evidence_revision: CASE.evidence_revision,
};

function fakeService(overrides: Partial<GoldenValidationService> = {}) {
  return {
    list: vi.fn(() => of([])),
    designate: vi.fn(() => of(CASE)),
    review: vi.fn(() => of({
      ...CASE,
      state: "accepted_reviewed_deviations",
      latest_review: REVIEW,
      reviews: [],
    })),
    ...overrides,
  };
}

describe("GoldenValidationWorkbenchComponent", () => {
  it("designates a Python history run only after the baseline note is supplied", async () => {
    const service = fakeService();
    await render(GoldenValidationWorkbenchComponent, {
      inputs: { sourceRunId: 44 },
      providers: [
        provideZonelessChangeDetection(),
        { provide: GoldenValidationService, useValue: service },
      ],
    });
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Designate golden run" }));
    expect(screen.getByText(/Explain why this is the selected validation baseline/i)).toBeTruthy();

    await user.type(screen.getByRole("textbox", { name: /Why is this/i }), "Selected after parameter exploration.");
    await user.click(screen.getByRole("button", { name: "Designate golden run" }));

    await waitFor(() => expect(service.designate).toHaveBeenCalled());
    expect(service.designate).toHaveBeenCalledWith(expect.objectContaining({ source_run_id: 44 }));
    expect(await screen.findByText("Frozen validation case")).toBeTruthy();
    expect(screen.getByText("Computed engine evidence")).toBeTruthy();
  });

  it("keeps computed deviations visible when a reviewer accepts them", async () => {
    const service = fakeService({ list: vi.fn(() => of([CASE])) });
    await render(GoldenValidationWorkbenchComponent, {
      providers: [
        provideZonelessChangeDetection(),
        { provide: GoldenValidationService, useValue: service },
      ],
    });
    const user = userEvent.setup();

    expect(await screen.findByText("Computed engine evidence")).toBeTruthy();
    expect(screen.getByText("Deviations")).toBeTruthy();
    expect(screen.getByText(/minute/i)).toBeTruthy();
    await user.click(screen.getByText("Exact parameters, data, and execution scope"));
    expect(screen.getByText(/Compatibility Profile/)).toBeTruthy();
    expect(screen.getByText("Us Equity Raw IBKR V1")).toBeTruthy();
    expect(screen.getByText("spy-fixture")).toBeTruthy();
    expect(screen.getByText("a4f729d1e8c0")).toBeTruthy();
    expect(screen.getByText("references/lean/ema-crossover.py")).toBeTruthy();
    await user.type(screen.getByRole("textbox", { name: "Review note" }), "Reviewed the extra LEAN trade.");
    await user.click(screen.getByRole("button", { name: "Save review" }));

    await waitFor(() => expect(service.review).toHaveBeenCalledWith(7, expect.objectContaining({
      decision: "accept",
      expected_evidence_revision: CASE.evidence_revision,
    })));
    expect(screen.getByText("Deviations")).toBeTruthy();
    expect(await screen.findByText(/Golden validation accepted as Reviewed Deviations/i)).toBeTruthy();
  });

  it("shows an explicit absence when execution configuration was not recorded", async () => {
    const withoutExecutionConfiguration: GoldenValidation = {
      ...CASE,
      validation_case: {
        ...CASE.validation_case,
        execution: { ...CASE.validation_case.execution, configuration: null },
      },
    };
    const service = fakeService({ list: vi.fn(() => of([withoutExecutionConfiguration])) });
    await render(GoldenValidationWorkbenchComponent, {
      providers: [
        provideZonelessChangeDetection(),
        { provide: GoldenValidationService, useValue: service },
      ],
    });
    const user = userEvent.setup();

    await user.click(await screen.findByText("Exact parameters, data, and execution scope"));

    expect(screen.getByText("Configuration")).toBeTruthy();
    expect(screen.getByText("Not recorded")).toBeTruthy();
  });

  it("reuses the same review command when a response is lost and the intent is unchanged", async () => {
    const commands: string[] = [];
    const review = vi.fn((_id: number, request: ReviewGoldenValidationRequest) => {
      commands.push(request.command_id);
      return throwError(() => new Error("response lost"));
    });
    const service = fakeService({ list: vi.fn(() => of([CASE])), review });
    await render(GoldenValidationWorkbenchComponent, {
      providers: [
        provideZonelessChangeDetection(),
        { provide: GoldenValidationService, useValue: service },
      ],
    });
    const user = userEvent.setup();

    await user.type(await screen.findByRole("textbox", { name: "Review note" }), "Reviewed the evidence.");
    await user.click(screen.getByRole("button", { name: "Save review" }));
    await waitFor(() => expect(service.review).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "Refresh evidence" }));
    await waitFor(() => expect(service.list).toHaveBeenCalledTimes(2));
    await user.click(screen.getByRole("button", { name: "Save review" }));
    await waitFor(() => expect(service.review).toHaveBeenCalledTimes(2));

    expect(commands[0]).toBe(commands[1]);
  });

  it("reloads the dossier when review is refused because evidence changed", async () => {
    const refreshed = {
      ...CASE,
      evidence_state: "pending",
      evidence_revision: "b".repeat(64),
      parity_evidence: {
        computed_state: "pending",
        qualification_warnings: [],
        parity_verdict: { status: "pending" },
      },
    } satisfies GoldenValidation;
    const list = vi.fn()
      .mockReturnValueOnce(of([CASE]))
      .mockReturnValue(of([refreshed]));
    const review = vi.fn(() => throwError(() => new HttpErrorResponse({
      status: 409,
      error: { detail: { code: "STALE_GOLDEN_VALIDATION_EVIDENCE" } },
    })));
    const service = fakeService({ list, review });
    await render(GoldenValidationWorkbenchComponent, {
      providers: [
        provideZonelessChangeDetection(),
        { provide: GoldenValidationService, useValue: service },
      ],
    });
    const user = userEvent.setup();

    await user.type(await screen.findByRole("textbox", { name: "Review note" }), "Reviewed the evidence.");
    await user.click(screen.getByRole("button", { name: "Save review" }));

    expect(await screen.findByText(/engine evidence changed/i)).toBeTruthy();
    await waitFor(() => expect(service.list).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("heading", { name: "Pending" })).toBeTruthy();
  });

  it("warns when the latest review belongs to an older evidence revision", async () => {
    const stale = {
      ...CASE,
      state: "accepted_reviewed_deviations",
      latest_review: REVIEW,
      review_is_current: false,
      reviews: [REVIEW],
    } satisfies GoldenValidation;
    const service = fakeService({ list: vi.fn(() => of([stale])) });

    await render(GoldenValidationWorkbenchComponent, {
      providers: [
        provideZonelessChangeDetection(),
        { provide: GoldenValidationService, useValue: service },
      ],
    });

    const warning = await screen.findByText(/decision reviewed an earlier evidence revision/i);
    expect(warning.getAttribute("role")).toBe("alert");
  });
});
