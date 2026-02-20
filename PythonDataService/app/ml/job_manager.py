from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class JobManager:
    """In-memory async job store for long-running ML tasks."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}

    def submit(self, job_fn: Callable[..., Any], **kwargs: Any) -> str:
        """Submit a sync function to run in a background thread.

        Returns the job_id immediately.
        """
        job_id = str(uuid.uuid4())
        self._jobs[job_id] = {
            "status": JobStatus.PENDING,
            "result": None,
            "error": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None,
        }

        loop = asyncio.get_event_loop()
        loop.create_task(self._run_job(job_id, job_fn, **kwargs))

        logger.info(f"[ML] Job {job_id} submitted")
        return job_id

    async def _run_job(
        self, job_id: str, job_fn: Callable[..., Any], **kwargs: Any
    ) -> None:
        """Execute the job function in a thread pool and update status."""
        self._jobs[job_id]["status"] = JobStatus.RUNNING
        logger.info(f"[ML] Job {job_id} started")

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, lambda: job_fn(**kwargs))
            self._jobs[job_id]["status"] = JobStatus.COMPLETED
            self._jobs[job_id]["result"] = result
            self._jobs[job_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
            logger.info(f"[ML] Job {job_id} completed")
        except Exception as e:
            self._jobs[job_id]["status"] = JobStatus.FAILED
            self._jobs[job_id]["error"] = str(e)
            self._jobs[job_id]["completed_at"] = datetime.now(timezone.utc).isoformat()
            logger.error(f"[ML] Job {job_id} failed: {e}")

    def get_status(self, job_id: str) -> dict[str, Any] | None:
        """Get job status and results."""
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict[str, Any]]:
        """List all jobs with metadata."""
        return [
            {"job_id": jid, **info} for jid, info in self._jobs.items()
        ]


# Module-level singleton
job_manager = JobManager()
