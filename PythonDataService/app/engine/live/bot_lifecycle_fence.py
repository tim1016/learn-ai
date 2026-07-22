"""The cross-process fence for one strategy instance's lifecycle decisions."""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from pathlib import Path

from app.engine.live.identity import validate_strategy_instance_id
from app.engine.live.live_state_sidecar import _file_lock

BOT_LIFECYCLE_OPERATION_FENCE_FILENAME = "lifecycle_operation_fence"


def stable_bot_lifecycle_operation_fence_path(
    artifacts_root: Path,
    strategy_instance_id: str,
) -> Path:
    """Return the durable cross-writer fence for one bot identity."""

    validate_strategy_instance_id(strategy_instance_id)
    return artifacts_root / "live_state" / strategy_instance_id / BOT_LIFECYCLE_OPERATION_FENCE_FILENAME


@contextlib.contextmanager
def bot_lifecycle_operation_fence(artifacts_root: Path, strategy_instance_id: str) -> Iterable[None]:
    """Serialize every broker, admission, duty, and retirement transition."""

    path = stable_bot_lifecycle_operation_fence_path(artifacts_root, strategy_instance_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(path):
        yield


__all__ = [
    "BOT_LIFECYCLE_OPERATION_FENCE_FILENAME",
    "bot_lifecycle_operation_fence",
    "stable_bot_lifecycle_operation_fence_path",
]
