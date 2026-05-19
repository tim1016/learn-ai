import { ComponentFixture, TestBed } from "@angular/core/testing";
import { describe, it, expect, beforeEach, vi } from "vitest";

import { BlockedAwareDatePickerComponent } from "./blocked-aware-date-picker.component";
import { LeanSidecarService } from "../../../services/lean-sidecar.service";
import type { BlockedDatesPayload } from "../../../services/lean-sidecar.types";

/**
 * P2.5 picker tests mirror the design hub's six states. The calendar
 * service is mocked with a payload that includes one weekend, one
 * holiday, and one half-day so the three disabled-state branches are
 * each exercised.
 */

class FakeLeanSidecarService {
  payload: BlockedDatesPayload = {
    from: "2025-12-01",
    to: "2026-01-31",
    blocked: [
      // Christmas Eve half-day 2025-12-24.
      { date: "2025-12-24", reason: "early_close" },
      // Christmas full-day holiday 2025-12-25.
      { date: "2025-12-25", reason: "holiday" },
      // Saturday 2025-12-27.
      { date: "2025-12-27", reason: "weekend" },
    ],
  };
  getBlockedDates = vi.fn(async () => this.payload);
}

describe("BlockedAwareDatePickerComponent", () => {
  let fixture: ComponentFixture<BlockedAwareDatePickerComponent>;
  let component: BlockedAwareDatePickerComponent;
  let fake: FakeLeanSidecarService;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    fake = new FakeLeanSidecarService();
    await TestBed.configureTestingModule({
      imports: [BlockedAwareDatePickerComponent],
      providers: [{ provide: LeanSidecarService, useValue: fake }],
    }).compileComponents();

    fixture = TestBed.createComponent(BlockedAwareDatePickerComponent);
    fixture.componentRef.setInput("startDate", "2025-12-22");
    fixture.componentRef.setInput("endDate", "2025-12-26");
    component = fixture.componentInstance;
    fixture.detectChanges();
    // Allow the in-constructor blocked-dates fetch to resolve.
    await Promise.resolve();
    await Promise.resolve();
    fixture.detectChanges();
  });

  it("flags an invalid window when the range touches a half-day", () => {
    // 2025-12-24 (half-day) sits between start 12-22 and end 12-26.
    expect(component.invalidWindow()).toBe(true);
    const ad = component.advisory();
    expect(ad).not.toBeNull();
    expect(ad?.kind).toBe("bad");
    expect(ad?.text).toContain("half-day");
    expect(ad?.text).toContain("2025-12-24");
  });

  it("accepts a clean window with no blocked dates", async () => {
    fixture.componentRef.setInput("startDate", "2026-01-05");
    fixture.componentRef.setInput("endDate", "2026-01-09");
    fixture.detectChanges();
    expect(component.invalidWindow()).toBe(false);
    expect(component.advisory()).toBeNull();
  });

  it("computes exclusiveEndIso = next trading day after endDate", async () => {
    fixture.componentRef.setInput("startDate", "2026-01-05");
    fixture.componentRef.setInput("endDate", "2026-01-09"); // Friday
    fixture.detectChanges();
    // Sat/Sun 10/11 are blocked weekends in the test fixture? In the
    // fake payload they aren't listed — but the picker only walks
    // forward until a date that is NOT in the map. The fake fixture
    // doesn't include Jan 2026 weekend rows, so the next day Jan 10
    // (Sat) is treated as available, demonstrating the picker honors
    // the server's payload verbatim. Extend the fake to include Jan
    // weekends so the assertion proves the skip behavior.
    fake.payload = {
      from: "2025-12-01",
      to: "2026-01-31",
      blocked: [
        { date: "2026-01-10", reason: "weekend" },
        { date: "2026-01-11", reason: "weekend" },
      ],
    };
    fake.getBlockedDates = vi.fn(async () => fake.payload);
    await component["refreshBlockedDates"]();
    fixture.detectChanges();
    expect(component.exclusiveEndIso()).toBe("2026-01-12");
  });

  it("session-open ms conversion produces different values across DST", () => {
    // 2026-03-06 (EST) and 2026-03-09 (EDT). The picker's internal
    // utcOffsetMinutes drives both, so a fixed-offset bug would emit
    // identical-modulo-3-days values; the correct DST-aware code
    // produces a 1-hour delta.
    fixture.componentRef.setInput("startDate", "2026-03-06");
    fixture.componentRef.setInput("endDate", "2026-03-09");
    fixture.detectChanges();
    const startMs = component.startMsUtc();
    const endMs = component.endMsUtc();
    // exclusiveEnd is 2026-03-10 (Tue) absent a weekend block; both
    // start and end resolve through different DST offsets.
    expect(endMs - startMs).not.toBe(4 * 86_400_000);
    // The actual gap is 4 days minus 1 DST hour = 4 * 86_400_000 - 3_600_000.
    expect(endMs - startMs).toBe(4 * 86_400_000 - 3_600_000);
  });

  it("dstAdvisory fires when the window straddles EST/EDT", () => {
    fixture.componentRef.setInput("startDate", "2026-03-06");
    fixture.componentRef.setInput("endDate", "2026-03-09");
    fixture.detectChanges();
    expect(component.dstAdvisory()).not.toBeNull();
  });

  it("dstAdvisory stays null inside a single DST regime", () => {
    fixture.componentRef.setInput("startDate", "2026-01-05");
    fixture.componentRef.setInput("endDate", "2026-01-09");
    fixture.detectChanges();
    expect(component.dstAdvisory()).toBeNull();
  });

  it("month grid disables holidays and half-days", () => {
    component.viewMonth.set("2025-12-01");
    fixture.detectChanges();
    const dec24 = component.monthCells().find((c) => c.iso === "2025-12-24");
    const dec25 = component.monthCells().find((c) => c.iso === "2025-12-25");
    const dec22 = component.monthCells().find((c) => c.iso === "2025-12-22");
    expect(dec24?.reason).toBe("early_close");
    expect(dec25?.reason).toBe("holiday");
    expect(dec22?.reason).toBeNull(); // Monday, available
  });

  it("clicking a cell emits the picker selection", () => {
    component.openPopoverFor("start");
    component.onCellClick("2026-01-06");
    expect(component.startDate()).toBe("2026-01-06");
    // After picking a start, the popover switches focus to end.
    expect(component.openFor()).toBe("end");
  });
});
