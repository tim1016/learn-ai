"""Typed representation of a strategy registration's parameter JSON schema.

Wraps the flat, non-recursive shape ``registration.param_schema.model_json_schema()``
actually produces for every current strategy registration: leaf ``str`` / ``int`` /
``float`` fields with simple ``Field(...)`` constraints, no ``$defs``, ``anyOf``, or
nested submodels. Modeling it here -- instead of leaving it ``dict[str, Any]`` -- lets
a consumer get a real generated TypeScript type at the OpenAPI boundary rather than
the codegen's blanket ``Record<string, never>`` narrowing for untyped dicts.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ParamPropertySchema(BaseModel):
    """One parameter's JSON-schema leaf metadata."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    type: str | None = None
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None
    description: str | None = None
    title: str | None = None


class StrategyParamsSchema(BaseModel):
    """A strategy registration's public parameter schema."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    title: str | None = None
    type: str | None = None
    properties: dict[str, ParamPropertySchema] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)
