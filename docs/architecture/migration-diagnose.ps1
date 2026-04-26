# migration-diagnose.ps1 — read-only diagnostic for the feature branch state.
# Run from C:\Users\inkan\learn-ai\ and paste the full output back.
$ErrorActionPreference = "Continue"

function Section($title) {
    Write-Output ""
    Write-Output ("=" * 80)
    Write-Output "=== $title"
    Write-Output ("=" * 80)
}

Section "Branch + recent log"
git branch --show-current
Write-Output ""
git --no-pager log --oneline -8

Section "ahead/behind origin/master"
git --no-pager rev-list --left-right --count HEAD...origin/master

Section "Working-tree diff against HEAD — overall stats"
git --no-pager diff --stat HEAD

Section "Working-tree diff — counting lines per file (excluding mode-only)"
git --no-pager diff --numstat HEAD

Section "File-mode-only changes (likely sandbox artifact, harmless)"
git --no-pager diff HEAD | Select-String -Pattern '^old mode|^new mode' | Select-Object -First 40

Section "Sample diff — Backend/Services/Implementation/PortfolioRiskService.cs (first 80 lines)"
git --no-pager diff HEAD -- Backend/Services/Implementation/PortfolioRiskService.cs | Select-Object -First 80

Section "Sample diff — PythonDataService/app/services/strategy_engine.py (first 60 lines)"
git --no-pager diff HEAD -- PythonDataService/app/services/strategy_engine.py | Select-Object -First 60

Section "Sample diff — Frontend/src/app/components/options-strategy-lab/options-strategy-lab.component.ts (first 60 lines)"
git --no-pager diff HEAD -- Frontend/src/app/components/options-strategy-lab/options-strategy-lab.component.ts | Select-Object -First 60

Section "git status (short form)"
git --no-pager status --short

Section "Truly-untracked prior-session artifacts (first 4 lines of each)"
$artifacts = @(
    "docs/architecture/backtesting-engine-grounding-2026-04-26.md",
    "docs/architecture/engine-authority-matrix.md",
    "docs/architecture/polygon-integration-grounding-2026-04-26.md",
    "docs/architecture/desktop.md"
)
foreach ($f in $artifacts) {
    Write-Output ""
    Write-Output "--- $f"
    if (Test-Path $f) {
        Get-Content $f -TotalCount 4
        $size = (Get-Item $f).Length
        Write-Output "(size: $size bytes)"
    } else {
        Write-Output "(missing)"
    }
}

Section "Sample of .agents/ vs .claude/skills/ (compare a single skill)"
$a = ".agents/skills/port-indicator/SKILL.md"
$b = ".claude/skills/port-indicator/SKILL.md"
if ((Test-Path $a) -and (Test-Path $b)) {
    $diffCount = (Compare-Object (Get-Content $a) (Get-Content $b)).Count
    Write-Output "lines that differ: $diffCount"
    if ($diffCount -eq 0) { Write-Output "→ .agents/ is a byte-identical copy of .claude/skills/" }
} else {
    Write-Output "(one of the comparison files is missing)"
}

Section "Stash list"
git --no-pager stash list

Section "DONE — paste everything from 'Branch + recent log' onward back"