import { fireEvent, render, screen, waitFor } from "@testing-library/angular";
import { afterEach, describe, expect, it, vi } from "vitest";

import { QuantConnectReferenceCodeComponent } from "./quantconnect-reference-code.component";

const REFERENCE_CODE = {
  path: "references/qc-shadow/SpyEmaCrossoverAlgorithm.py",
  sha256: "cfc7f18877b8dcf9b99af4bb26e4f36f0b7ac6799fa5f4d6dc286945653d6078",
  recorded_sha256:
    "cfc7f18877b8dcf9b99af4bb26e4f36f0b7ac6799fa5f4d6dc286945653d6078",
  state: "current" as const,
  language: "python",
  source: "class SpyEmaCrossoverAlgorithm(QCAlgorithm):\n    pass\n",
};

function stubClipboard(writeText: ReturnType<typeof vi.fn>): void {
  const navigatorWithClipboard = Object.create(window.navigator) as Navigator;
  Object.defineProperty(navigatorWithClipboard, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  vi.stubGlobal("navigator", navigatorWithClipboard);
}

describe("QuantConnectReferenceCodeComponent", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("copies the exact SHA-pinned audit copy for a QuantConnect backtest", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    stubClipboard(writeText);
    await render(QuantConnectReferenceCodeComponent, {
      inputs: { referenceCode: REFERENCE_CODE },
    });

    expect(
      screen.getByRole("heading", { name: "QuantConnect reference algorithm" }),
    ).toBeTruthy();
    expect(
      screen.getByText("references/qc-shadow/SpyEmaCrossoverAlgorithm.py"),
    ).toBeTruthy();
    expect(screen.getAllByText(REFERENCE_CODE.sha256)).toHaveLength(2);

    const copyButton = screen.getByRole("button", {
      name: "Copy QuantConnect algorithm",
    });
    fireEvent.click(copyButton);

    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith(REFERENCE_CODE.source);
      expect(copyButton.textContent).toContain("Copied");
    });
  });

  it("distinguishes a stale current hash from the recorded reference hash", async () => {
    const currentSha = "a".repeat(64);
    await render(QuantConnectReferenceCodeComponent, {
      inputs: {
        referenceCode: {
          ...REFERENCE_CODE,
          sha256: currentSha,
          state: "stale",
        },
      },
    });

    expect(screen.getByRole("status").textContent).toContain(
      "differs from the copy used by the recorded reference run",
    );
    expect(screen.getByText(currentSha)).toBeTruthy();
    expect(screen.getByText(REFERENCE_CODE.recorded_sha256)).toBeTruthy();
  });

  it("explains when the browser blocks clipboard access", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("clipboard denied"));
    stubClipboard(writeText);
    await render(QuantConnectReferenceCodeComponent, {
      inputs: { referenceCode: REFERENCE_CODE },
    });

    fireEvent.click(
      screen.getByRole("button", { name: "Copy QuantConnect algorithm" }),
    );

    expect((await screen.findByRole("alert")).textContent).toContain(
      "Copy was blocked",
    );
  });
});
