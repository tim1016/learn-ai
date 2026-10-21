"""Response bodies of the public jobs surface (``/api/jobs``, :mod:`app.routers.jobs_facade`).

The start body is not modelled: it is whatever the job type's internal route
accepts, and the facade passes it through verbatim.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class JobAcceptedResponse(BaseModel):
    id: str = Field(description="The minted job id; its progress is the SSE stream at /api/jobs/{id}/events.")
    status: Literal["queued"] = "queued"


class JobCancelResponse(BaseModel):
    id: str
    cancel_requested: bool = Field(description="Always true: the flag is set and the worker finishes its own record.")


class JobErrorResponse(BaseModel):
    error: str


class UnsupportedJobTypeResponse(BaseModel):
    error: str
    supported: list[str] = Field(description="Every job type this surface starts.")


class JobRejectedProblem(BaseModel):
    """A ProblemDetails-shaped refusal: the worker route's own status, its raw body as ``detail``."""

    title: str
    status: int
    detail: str = Field(description="The worker route's response body text, or the transport error behind a 502.")
