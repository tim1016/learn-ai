import { provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { afterEach, describe, expect, it } from "vitest";

import { ValidationScopeNoteComponent } from "./validation-scope-note.component";

afterEach(() => {
  document.body.querySelectorAll(".p-dialog-mask").forEach((element) => element.remove());
});

async function renderNote() {
  await TestBed.configureTestingModule({
    imports: [ValidationScopeNoteComponent],
    providers: [provideZonelessChangeDetection()],
  }).compileComponents();
  const fixture = TestBed.createComponent(ValidationScopeNoteComponent);
  fixture.detectChanges();
  return fixture;
}

describe("ValidationScopeNoteComponent", () => {
  it("names the trigger accessibly and marks it as a dialog opener", async () => {
    const fixture = await renderNote();

    const trigger = (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>("button");
    expect(trigger).not.toBeNull();
    expect(trigger?.textContent).toContain("LEAN validation scope");
    expect(trigger?.getAttribute("aria-haspopup")).toBe("dialog");
  });

  it("opens the explainer and states the summary-level validation contract", async () => {
    const fixture = await renderNote();

    (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>("button")?.click();
    fixture.detectChanges();

    expect(document.body.textContent).toContain("What LEAN validation covers");
    expect(document.body.textContent).toContain("Validation is summary-level, by design.");
    expect(document.body.textContent).toContain("closed-trade ledger");
    expect(document.body.textContent).toContain("Individual minute bars are never validated");
    expect(document.body.textContent).toContain("expected, not a defect");
  });

  it("closes the explainer from the dialog's own close control", async () => {
    const fixture = await renderNote();

    (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>("button")?.click();
    fixture.detectChanges();
    expect(document.body.textContent).toContain("What LEAN validation covers");

    const close = document.body.querySelector<HTMLButtonElement>(".validation-scope-dialog .p-dialog-close-button");
    close?.click();
    fixture.detectChanges();

    expect(document.body.textContent).not.toContain("Validation is summary-level, by design.");
  });
});
