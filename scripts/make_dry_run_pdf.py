"""One-shot generator for the IBKR paper dry-run instructions PDF.

Run: python scripts/make_dry_run_pdf.py
Output: docs/runbooks/ibkr-paper-dry-run.pdf
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title", parent=base["Title"], fontSize=20, spaceAfter=8, textColor=colors.HexColor("#0b3b5e"),
        ),
        "subtitle": ParagraphStyle(
            "subtitle", parent=base["Normal"], fontSize=10, textColor=colors.grey, spaceAfter=18,
        ),
        "h2": ParagraphStyle(
            "h2", parent=base["Heading2"], fontSize=14, spaceBefore=14, spaceAfter=6,
            textColor=colors.HexColor("#0b3b5e"),
        ),
        "h3": ParagraphStyle(
            "h3", parent=base["Heading3"], fontSize=11, spaceBefore=8, spaceAfter=4,
            textColor=colors.HexColor("#444"),
        ),
        "body": ParagraphStyle(
            "body", parent=base["BodyText"], fontSize=10, leading=14, spaceAfter=6,
            alignment=TA_LEFT,
        ),
        "code": ParagraphStyle(
            "code", parent=base["BodyText"], fontName="Courier", fontSize=8.5, leading=11,
            leftIndent=10, rightIndent=10, spaceBefore=4, spaceAfter=8,
            backColor=colors.HexColor("#f3f4f6"),
            borderColor=colors.HexColor("#dadcdf"), borderWidth=0.5,
            borderPadding=6,
        ),
        "note": ParagraphStyle(
            "note", parent=base["BodyText"], fontSize=9.5, leading=13,
            leftIndent=10, rightIndent=10, spaceBefore=2, spaceAfter=8,
            backColor=colors.HexColor("#fff8e1"),
            borderColor=colors.HexColor("#f1c40f"), borderWidth=0.5,
            borderPadding=6,
        ),
    }


def main() -> None:
    out = Path(__file__).resolve().parents[1] / "docs" / "runbooks" / "ibkr-paper-dry-run.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)

    s = _styles()
    doc = SimpleDocTemplate(
        str(out), pagesize=LETTER,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
        title="IBKR Paper Dry Run",
        author="learn-ai",
    )

    story: list = []

    def P(text: str, style: str = "body") -> None:
        story.append(Paragraph(text, s[style]))

    def CODE(text: str) -> None:
        # Escape angle brackets for reportlab's mini-HTML.
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        # Preserve newlines as <br/>.
        escaped = escaped.replace("\n", "<br/>")
        story.append(Paragraph(escaped, s["code"]))

    def NOTE(text: str) -> None:
        story.append(Paragraph(text, s["note"]))

    def HR() -> None:
        story.append(Spacer(1, 4))
        story.append(HRFlowable(width="100%", color=colors.lightgrey, thickness=0.4))
        story.append(Spacer(1, 4))

    # ─── Header
    P("IBKR Paper Dry Run", "title")
    P(
        "Phase D one-day rehearsal against IB Gateway in <b>read-only</b> mode. "
        "No real orders are placed — the goal is to verify every piece of plumbing works "
        "before the 15-day paper week starts.",
        "subtitle",
    )

    # ─── Setup
    P("Before you start", "h2")
    P(
        "<b>You've already done</b> <font face='Courier' size='9'>podman compose up -d</font>. "
        "Confirm these last few prereqs:"
    )
    P(
        "• IB Gateway is running, paper account, port 4002 (NOT 4001).<br/>"
        "• Account ID starts with <font face='Courier' size='9'>DU</font>.<br/>"
        "• <font face='Courier' size='9'>.env</font> has the IBKR settings "
        "(<font face='Courier' size='9'>IBKR_MODE=paper</font>, "
        "<font face='Courier' size='9'>IBKR_PORT=4002</font>, "
        "<font face='Courier' size='9'>IBKR_CLIENT_ID=42</font>, "
        "<font face='Courier' size='9'>IBKR_READONLY=true</font>).<br/>"
        "• Source tree is clean: "
        "<font face='Courier' size='9'>git status -- PythonDataService references/qc-shadow</font> "
        "is empty.<br/>"
        "• Activate the host venv once per shell:"
    )
    CODE(
        "cd PythonDataService\n"
        "source .venv/Scripts/activate    # Git Bash on Windows\n"
        "# or:  .venv\\Scripts\\Activate.ps1   (PowerShell)\n"
        "cd .."
    )
    P("Set these shell variables once. The steps below reference them as <font face='Courier' size='9'>$ACCOUNT</font> and <font face='Courier' size='9'>$RUN_ID</font> so you don't paste raw angle-bracket placeholders that bash treats as redirects.")
    CODE(
        "export ACCOUNT='DU1234567'   # ← replace with your DU paper account\n"
        "export PYTHONPATH=PythonDataService\n"
        "# RUN_ID is set after Step 1; Steps 2 and 4 use it."
    )

    # ─── Step 1
    HR()
    P("Step 1 — Initialize the dry-run ledger (HOST)", "h2")
    P(
        "<b>What:</b> writes <font face='Courier' size='9'>run_ledger.json</font> with the run identity "
        "(strategy spec hash, QC audit copy hash, account ID, start-of-session UTC ms)."
    )
    P(
        "<b>Why:</b> the ledger is the canonical fingerprint for this run. The hashes in it "
        "appear in every reconciliation receipt, so a future operator can verify exactly what "
        "code and what spec produced the day's output. The init-ledger step refuses if your "
        "tree is dirty — that's how <font face='Courier' size='9'>code_sha</font> stays meaningful."
    )
    P(
        "Single-line command for clean copy-paste — line wraps in the PDF "
        "are display only:"
    )
    CODE(
        "python -m app.engine.live.run init-ledger --repo-root . --clean-tree-scope "
        "PythonDataService references/qc-shadow --strategy-spec-path "
        "PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json "
        "--qc-audit-copy-path references/qc-shadow/SpyEmaCrossoverAlgorithm.py "
        "--qc-cloud-backtest-id dry-run-no-cloud --account-id \"$ACCOUNT\" "
        "--start-date-ms $(date -u -d \"today 09:30 EDT\" +%s000) "
        "--live-config-json '{\"symbol\":\"SPY\",\"force_flat_at\":\"15:55\","
        "\"client_id\":42}' --run-root PythonDataService/artifacts/live_runs"
    )
    P(
        "<b>Expect:</b> a single stdout line of the form "
        "<font face='Courier' size='9'>[INIT-LEDGER] wrote ... (run_id=&lt;hex64&gt;)</font>. "
        "Capture the run_id into a shell variable — Steps 2 and 4 reference it:"
    )
    CODE("export RUN_ID='paste_the_64-char_hex_here'")

    # ─── Step 2
    HR()
    P("Step 2 — Pre-flight gate (HOST)", "h2")
    P(
        "<b>What:</b> runs the morning halt checks — clean tree, NTP offset, "
        "<font face='Courier' size='9'>run_ledger.json</font> intact, no leftover "
        "<font face='Courier' size='9'>halt.flag</font> from a prior session."
    )
    P(
        "<b>Why:</b> this is the same gate that fires every morning during paper week. "
        "If any check fails, paper week is paused for the day. The dry run "
        "rehearses it so you know what \"green\" looks like before doing it for real."
    )
    CODE(
        "python -m app.engine.live.run pre-flight --repo-root . --clean-tree-scope "
        "PythonDataService references/qc-shadow --run-dir "
        "PythonDataService/artifacts/live_runs/$RUN_ID"
    )
    P(
        "<b>Expect:</b> every check prints <font face='Courier' size='9'>OK</font>, "
        "ending with <font face='Courier' size='9'>all checks passed; runner may proceed</font>. "
        "If <font face='Courier' size='9'>ntp_offset</font> fails because of a corporate firewall, "
        "add <font face='Courier' size='9'>--skip-ntp</font> for the dry run only "
        "(NOT for paper week — fix the firewall first)."
    )

    # ─── Step 3
    HR()
    P("Step 3 — Read-only run (CONTAINER)", "h2")
    P(
        "<b>What:</b> connects to IB Gateway, subscribes to SPY 5-second TRADES bars, "
        "consolidates to 15-min, runs the strategy. <font face='Courier' size='9'>--readonly</font> "
        "short-circuits <font face='Courier' size='9'>place_order</font> so no broker orders go out."
    )
    P(
        "<b>Why:</b> this is the only step that needs the container — the IBKR Gateway "
        "sidecar network lives there. Run it through a full session (or a synthetic replay window) "
        "to prove the bar stream, consolidator, decision logger, and writer pipeline all work end-to-end."
    )
    CODE(
        "podman exec polygon-data-service python -m app.engine.live.run start "
        "--run-dir /app/artifacts/live_runs/$RUN_ID --readonly"
    )
    P(
        "<b>Expect during the run:</b> IB Gateway shows one connected client (id=42); "
        "<font face='Courier' size='9'>decisions.parquet</font> grows by one row every 15 minutes; "
        "<font face='Courier' size='9'>executions.parquet</font> stays empty (correct — readonly). "
        "If the strategy emits an ENTER/EXIT signal, "
        "<font face='Courier' size='9'>decisions.parquet</font> records it but no fill comes back."
    )
    NOTE(
        "<b>If a halt fires intra-day:</b> the runner stops, writes "
        "<font face='Courier' size='9'>halt.flag</font> or "
        "<font face='Courier' size='9'>poisoned.flag</font>, and exits non-zero. "
        "Inspect, decide if it's expected, then proceed."
    )

    # ─── Step 4
    HR()
    P("Step 4 — End-of-session reconciliation (HOST)", "h2")
    P(
        "<b>What:</b> compares the runner's <font face='Courier' size='9'>decisions.parquet</font> "
        "against a synthetic QC export, classifies every bar as "
        "<font face='Courier' size='9'>none</font> / <font face='Courier' size='9'>data</font> / "
        "<font face='Courier' size='9'>engine</font> divergence, and writes the day-0 Markdown "
        "receipt with a SHA-256 manifest of every artifact it summarizes."
    )
    P(
        "<b>Why:</b> proves the daily reconcile pipeline works end-to-end. The committed "
        "Markdown is your dry-run deliverable — it's the same shape the operator will eyeball "
        "every paper day."
    )
    P("First, build a tiny synthetic QC indicators export:")
    CODE(
        "export TODAY=$(date -u +%Y-%m-%d)\n"
        "mkdir -p PythonDataService/artifacts/qc-dry-run/$TODAY\n"
        "# Hand-craft indicators.csv from the runner's decisions.parquet\n"
        "# (columns: bar_close_ms, ema5, ema10, rsi, signal). Worked example:\n"
        "# docs/references/reconciliations/dry-run-2026-05-09/day-0.md"
    )
    P("Then run reconcile:")
    CODE(
        "python -m app.engine.live.reconcile --run-dir "
        "PythonDataService/artifacts/live_runs/$RUN_ID --qc-dir "
        "PythonDataService/artifacts/qc-dry-run/$TODAY --docs-dir "
        "docs/references/reconciliations/dry-run-$TODAY --run-label "
        "dry-run-$TODAY --day-n 0 --day-date $TODAY"
    )
    P(
        "<b>Expect:</b> all four artifacts written "
        "(<font face='Courier' size='9'>day-0.md</font>, <font face='Courier' size='9'>.json</font>, "
        "<font face='Courier' size='9'>.parquet</font>, <font face='Courier' size='9'>.hashes.json</font>). "
        "Markdown shows zero <b>cross-engine</b> divergences."
    )
    NOTE(
        "<b>Expected fill-class breach in readonly:</b> if the strategy emitted any signal in Step 3, "
        "the receipt will show <font face='Courier' size='9'>fill-class breach count=N</font> and "
        "write <font face='Courier' size='9'>halt.flag</font>. <b>This is normal in readonly mode</b> "
        "— readonly means no fills came back to match the ENTER/EXIT intent. The receipt is still "
        "valid. <b>Delete <font face='Courier' size='9'>halt.flag</font> before any subsequent "
        "pre-flight</b>, otherwise tomorrow's gate will refuse to proceed."
    )

    # ─── Step 5
    HR()
    P("Step 5 — Regression test (HOST)", "h2")
    P(
        "<b>What:</b> re-runs the live-engine test suite against the run-start commit."
    )
    P(
        "<b>Why:</b> confirms nothing on master regressed since you started. Run from the "
        "host because <font face='Courier' size='9'>tests/</font> isn't mounted into the "
        "container."
    )
    CODE("cd PythonDataService\npython -m pytest tests/engine/live/ -v\ncd ..")
    P(
        "<b>Expect:</b> ~165 passed, a few skipped (the QC-export-required test consumers "
        "wait for your QC Cloud Test 1/2 runs, which are a separate operator task)."
    )

    # ─── Success criteria
    HR()
    P("You're done with Phase D when…", "h2")
    P(
        "✓ Step 1 wrote a 64-char hex <font face='Courier' size='9'>run_id</font>.<br/>"
        "✓ Step 2 emitted only <font face='Courier' size='9'>OK</font> lines.<br/>"
        "✓ Step 3 ran a full session with no unexpected intra-day halts.<br/>"
        "✓ Step 4 produced <font face='Courier' size='9'>day-0.md</font> "
        "(fill-class breach counted as expected if signals fired).<br/>"
        "✓ Step 5 saw no regressions vs the prior baseline.<br/>"
        "✓ No <font face='Courier' size='9'>halt.flag</font> or "
        "<font face='Courier' size='9'>poisoned.flag</font> left in the run dir.<br/>"
    )

    # ─── If something goes wrong
    HR()
    P("If something goes wrong", "h2")
    P(
        "<b>Dry-tree halt:</b> don't <font face='Courier' size='9'>git stash</font> to make "
        "it pass — that breaks <font face='Courier' size='9'>code_sha</font> identity. "
        "Commit or revert in scope and re-run from Step 1.<br/>"
        "<b>NTP halt:</b> fix the network or pick a different "
        "<font face='Courier' size='9'>--ntp-server</font>. Don't "
        "<font face='Courier' size='9'>--skip-ntp</font> in paper week.<br/>"
        "<b>IB Gateway disconnects:</b> the runner reconnects with a 60s timeout. On timeout "
        "it halts and writes a partial reconciliation. Resuming after disconnect requires a "
        "fresh <font face='Courier' size='9'>run_id</font>.<br/>"
        "<b>Reconcile sees engine-class divergence on dry-run synthetic inputs:</b> that's a "
        "real bug in the classifier — file an issue and stop. Don't proceed to paper week."
    )

    # ─── Next steps
    HR()
    P("After Phase D", "h2")
    P(
        "Once Phase D is green, the remaining operator tasks are: (1) run QC Cloud Test 1 + "
        "Test 2 and commit the exports under "
        "<font face='Courier' size='9'>references/qc-shadow/backtests/</font> (this activates "
        "the skip-marked Test 1/2 consumers); (2) flip "
        "<font face='Courier' size='9'>IBKR_READONLY=false</font> in "
        "<font face='Courier' size='9'>.env</font>, re-run Step 1 to mint a new "
        "<font face='Courier' size='9'>run_id</font> for the live config delta, and start "
        "the 15-day paper week proper."
    )

    doc.build(story)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
