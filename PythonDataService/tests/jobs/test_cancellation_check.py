"""``CancellationCheck`` reads the flag on its first call (#2463).

The throttle used to swallow the first ``check_every_n - 1`` calls, so a job
that checks cancellation only a handful of times — at phase boundaries, like
the rule-based backtest and the LEAN run — never read Redis at all and its
Cancel control was inert. The first check now always reads; hot loops that
check per bar still opt into throttling with an explicit ``check_every_n``,
and both defaults are 1.
"""

from __future__ import annotations

import inspect

from app.jobs.progress import CancellationCheck
from app.jobs.runner import run_in_thread


class TestFirstCallCheckReadsTheFlag:
    def test_the_first_call_reads_redis_even_under_a_throttle(self, fake) -> None:
        fake.hset("job:job-1:state", mapping={"cancel_requested": "1"})

        check = CancellationCheck("job-1", check_every_n=1000)

        assert check.should_cancel() is True

    def test_the_first_call_reads_redis_when_nothing_is_requested(self, fake) -> None:
        fake.hset("job:job-2:state", mapping={"cancel_requested": "0"})

        check = CancellationCheck("job-2", check_every_n=1000)

        assert check.should_cancel() is False

    def test_throttling_still_applies_after_the_first_read(self, fake) -> None:
        fake.hset("job:job-3:state", mapping={"cancel_requested": "0"})
        check = CancellationCheck("job-3", check_every_n=1000)
        assert check.should_cancel() is False  # the one gated read

        # A cancel landing after the first read stays invisible until the
        # throttle window opens again — the documented opt-in cost a hot
        # loop accepts, unchanged by #2463.
        fake.hset("job:job-3:state", mapping={"cancel_requested": "1"})
        assert check.should_cancel() is False


class TestDefaults:
    def test_the_check_defaults_to_reading_every_call(self) -> None:
        assert CancellationCheck(job_id="job-4").check_every_n == 1

    def test_the_runner_defaults_to_reading_every_call(self) -> None:
        assert inspect.signature(run_in_thread).parameters["cancel_check_every_n"].default == 1


class TestCancelledJobEmitsJobCancelled:
    def test_a_pre_set_flag_ends_the_job_cancelled(self, fake) -> None:
        """The whole chain a LEAN run's pre-start cancel rides (#2463): the
        first check reads the flag, the work raises ``JobCancelled``, and the
        runner's sink emits ``job.cancelled`` — never ``job.failed``."""
        fake.job("job-5", "running")
        fake.hset("job:job-5:state", mapping={"cancel_requested": "1"})
        ran: list[str] = []

        def work(emit, cancel) -> dict:
            cancel.raise_if_cancelled()
            ran.append("must not get here")  # pragma: no cover - cancelled first
            return {}

        thread = run_in_thread("job-5", work, thread_name="test-cancel")
        thread.join(timeout=5)

        events = [event["type"] for event in fake.events("job-5")]
        assert ran == []
        assert "job.started" in events
        assert events[-1] == "job.cancelled"
        assert "job.failed" not in events

