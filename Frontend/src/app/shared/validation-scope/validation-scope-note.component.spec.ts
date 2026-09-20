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
  it("names the trigger accessibly, marks it as a dialog opener, and tracks expansion", async () => {
    const fixture = await renderNote();

    const trigger = (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>("button");
    expect(trigger).not.toBeNull();
    expect(trigger?.textContent).toContain("LEAN validation scope");
    expect(trigger?.getAttribute("aria-haspopup")).toBe("dialog");
    expect(trigger?.getAttribute("aria-expanded")).toBe("false");

    trigger?.click();
    fixture.detectChanges();
    expect(trigger?.getAttribute("aria-expanded")).toBe("true");
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

  it("labels the dialog for screen readers through the header template's aria id", async () => {
    const fixture = await renderNote();

    (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>("button")?.click();
    fixture.detectChanges();

    // PrimeNG points the dialog container's aria-labelledby at a generated
    // id; with a custom header template the built-in title span is not
    // rendered, so the template itself must carry that id or the dialog is
    // unnamed.
    const dialog = document.body.querySelector<HTMLElement>(".validation-scope-dialog");
    const labelledBy = dialog?.getAttribute("aria-labelledby");
    expect(labelledBy).toBeTruthy();
    expect(document.getElementById(labelledBy as string)?.textContent).toContain("What LEAN validation covers");
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
