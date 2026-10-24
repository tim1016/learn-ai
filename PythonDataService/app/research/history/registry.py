"""Every store the Research History reads, registered once.

Adding a store is adding one source here and one member to ``HistoryType``: the merge, the endpoint and the page's
type filter follow. A store that keeps results durably appears here the day it does (#3520: the five studies' own records); it is
never read through another store's source.
"""

from __future__ import annotations

from pathlib import Path

from app.research.history.artifact_sources import Baselines, MonteCarlos, SpecRuns, WalkForwards
from app.research.history.contract import HistorySource
from app.research.history.legacy_sources import FeatureExperiments, SignalReports
from app.research.history.study_sources import study_record_sources


def default_history_sources(artifacts_root: Path | None = None) -> tuple[HistorySource, ...]:
    """The registered stores, in the order the page lists them. ``artifacts_root`` is for tests; ``None`` is the real one."""
    return (
        SpecRuns(artifacts_root),
        WalkForwards(artifacts_root),
        MonteCarlos(artifacts_root),
        Baselines(artifacts_root),
        *study_record_sources(artifacts_root),
        SignalReports(),
        FeatureExperiments(),
    )
