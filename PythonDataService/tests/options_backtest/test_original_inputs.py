"""Original artifacts keep replay pins, hard bounds and immutable admission on warm reads."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.engine.data.decoded_cache import DecodedArtifactCache
from app.options_backtest import frozen_inputs
from app.options_backtest.frozen_inputs import FrozenRunData, FrozenRunInputs, freeze_run_inputs
from app.options_backtest.run import run_sessions
from app.options_bots.golden_proof import GoldenProofError
from app.research.golden_search.proof import BlobStore, ProofMismatchError
from tests.options_backtest._archived_data import archived_scene
from tests.options_backtest._run_data import HALF_DAY, THU, WED, chain_scene
from tests.options_backtest.setup_fixtures import setup


def test_originals_are_byte_identical_and_warm_replay_cannot_hide_tampering(tmp_path: Path) -> None:
    source = archived_scene(chain_scene(), tmp_path / "source")
    originals = {digest: source.blobs.get(digest) for digest in source.manifest.values()}
    frozen = freeze_run_inputs(setup(), (WED, THU), source, tmp_path / "frozen")
    store = BlobStore(Path(frozen.root))
    assert all(store.get(digest) == originals[digest] for digest in frozen.blob_digests)
    assert frozen.retained_bytes == sum(len(originals[digest]) for digest in frozen.blob_digests)
    assert frozen.verify_blobs() == {digest: len(originals[digest]) for digest in frozen.blob_digests}
    expected = run_sessions(setup(), (WED, THU), source)
    cache = DecodedArtifactCache(max_bytes=64 * 1024**2)
    for _ in range(2):
        assert run_sessions(setup(), (WED, THU), FrozenRunData(frozen, cache)) == expected
    assert cache.metrics()["hits"] > 0
    digest = next(iter(next(iter(frozen.sessions.values())).snapshot.manifest.values()))
    (Path(frozen.root) / digest[:2] / digest).write_bytes(b"replacement")
    with pytest.raises(ProofMismatchError):
        FrozenRunData(frozen, cache).listing(WED)
    assert all(source.blobs.get(digest) == payload for digest, payload in originals.items())


@pytest.mark.parametrize("bound", ["session", "run"])
def test_original_byte_caps_refuse_before_publishing_an_oversized_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound: str,
) -> None:
    source = archived_scene(chain_scene(), tmp_path / "source")
    baseline = freeze_run_inputs(setup(), (WED, THU), source, tmp_path / "baseline")
    first = next(iter(baseline.sessions.values())).snapshot.retained_bytes
    limit = first - 1 if bound == "session" else first
    monkeypatch.setattr(frozen_inputs, "MAX_SESSION_BYTES" if bound == "session" else "MAX_FROZEN_BYTES", limit)
    checkpoints, reservations = [], []
    with pytest.raises(ValueError, match=r"original.*byte bound"):
        freeze_run_inputs(setup(), (WED, THU), source, tmp_path / "refused",
                          on_checkpoint=checkpoints.append, reserve_bytes=reservations.append)
    assert len(checkpoints) == (0 if bound == "session" else 1)
    assert max(reservations, default=0) <= limit


def test_transitive_inventory_counts_each_original_blob_once_and_refuses_old_checkpoint(tmp_path: Path) -> None:
    source = archived_scene(chain_scene(), tmp_path / "source")
    frozen = freeze_run_inputs(setup(), (WED,), source, tmp_path / "frozen")
    anchor, session = next(iter(frozen.sessions.items()))
    repeated = frozen.model_copy(update={"sessions": {anchor: session, anchor + 86_400_000: session}})
    assert repeated.blob_digests == frozen.blob_digests
    assert repeated.verify_blobs() == frozen.verify_blobs()
    old = frozen.model_dump(mode="json")
    old.update(version="options-run-data/v1", recipe="admitted-options-run/v1",
               sessions={str(anchor): hashlib.sha256(b"former decoded codec").hexdigest()})
    with pytest.raises(ValueError, match=r"unsupported frozen input.*restart preparation"):
        FrozenRunInputs.model_validate(old)


def test_warm_inventory_cannot_reuse_a_decode_for_another_session(tmp_path: Path) -> None:
    source = archived_scene(chain_scene(), tmp_path / "source")
    frozen = freeze_run_inputs(setup(), (WED,), source, tmp_path / "frozen")
    anchor, session = next(iter(frozen.sessions.items()))
    malformed = frozen.model_copy(update={"sessions": {anchor + 86_400_000: session}})
    for warm in (False, True):
        cache = DecodedArtifactCache(max_bytes=64 * 1024**2)
        if warm:
            assert FrozenRunData(frozen, cache).listing(WED) is not None
        with pytest.raises(GoldenProofError):
            FrozenRunData(malformed, cache).listing(THU)


def test_an_early_close_session_replays_from_its_frozen_originals_exactly_like_its_source(tmp_path: Path) -> None:
    source = archived_scene(chain_scene(), tmp_path / "source")
    sessions = (WED, HALF_DAY)
    frozen = freeze_run_inputs(setup(), sessions, source, tmp_path / "frozen")
    expected = run_sessions(setup(), sessions, source)
    early = expected.sessions[-1]
    assert early.session_close_ms - early.session_open_ms == 210 * 60_000 and early.pins.option_days
    assert run_sessions(setup(), sessions, FrozenRunData(frozen, DecodedArtifactCache(max_bytes=64 * 1024**2))) == expected
