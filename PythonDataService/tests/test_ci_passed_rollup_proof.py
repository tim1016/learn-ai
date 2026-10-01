"""Deliberately red: proves the `CI passed` roll-up fails with a shard (#2751). Reverted next commit."""


def test_ci_passed_goes_red_when_a_shard_fails() -> None:
    assert False, "deliberate failure for the CI passed proof (#2751)"
