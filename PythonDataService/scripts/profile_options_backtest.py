"""Profile frozen SPY replay without downloading market data (#3417).

Run in the data service to freeze admitted lake inputs; after #3423 the same
command runs in the research container against its shared mounted lake. Pass
a frozen setup JSON, its ~480-session half-open ms range, a raw lake root,
an isolated input directory and an output text file. No provider calls occur.
The production benchmark separately measures acceptance through persistence;
this standard-library profile diagnoses canonical replay's cumulative cost.
"""
from __future__ import annotations

import argparse
import cProfile
import io
import pstats
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from app.engine.data.decoded_cache import DecodedArtifactCache
from app.options_backtest.data import RunData
from app.options_backtest.frozen_inputs import FrozenRunData, freeze_run_inputs
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.run import RunResult, run_sessions
from app.options_backtest.service import sessions_of_range
from app.options_backtest.setup import OptionsBacktestSetup


def profile_run(setup: OptionsBacktestSetup, sessions: Sequence[date], data: RunData,
                input_root: Path, output: Path) -> RunResult:
    frozen = freeze_run_inputs(setup, sessions, data, input_root)
    profile = cProfile.Profile()
    result = profile.runcall(run_sessions, setup, sessions, FrozenRunData(frozen, DecodedArtifactCache(max_bytes=64 * 1024**2)))
    text = io.StringIO()
    text.write(f"Frozen input inventory: {frozen.digest}\nSessions: {len(sessions)}\n")
    pstats.Stats(profile, stream=text).sort_stats("cumulative").print_stats(50)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text.getvalue())
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", required=True, type=Path)
    parser.add_argument("--start-ms", required=True, type=int)
    parser.add_argument("--end-ms", required=True, type=int)
    parser.add_argument("--lake-root", required=True, type=Path)
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    setup = OptionsBacktestSetup.model_validate_json(args.setup.read_text())
    profile_run(setup, sessions_of_range(args.start_ms, args.end_ms),
                LakeRunData(setup.underlying, lake_root=args.lake_root), args.input_root, args.output)


if __name__ == "__main__":
    main()
