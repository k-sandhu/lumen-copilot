"""Trusted, persisted administrator opt-in. No implicit tenant enablement."""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ClassificationControls(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    provider_approved: bool = False
    model: str = ""
    allowed_models: tuple[str, ...] = ()
    taxonomy_version: str = "1.0.0"
    per_call_ceiling_usd: Decimal | None = Field(default=None, gt=0)
    budget_usd: Decimal | None = Field(default=None, gt=0)
    input_tokens: int | None = Field(default=None, gt=0, le=65536)
    excerpt_tokens: int | None = Field(default=None, gt=0, le=32768)
    excerpt_chars: int | None = Field(default=None, gt=0, le=32768)
    input_bytes: int | None = Field(default=None, gt=0, le=262144)
    timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    retries: int | None = Field(default=None, ge=0, le=5)
    stage_retries: int | None = Field(default=None, ge=0, le=10)
    backoff_seconds: float | None = Field(default=None, gt=0, le=3600)
    concurrency: int | None = Field(default=None, ge=1, le=8)
    fallback_model: str | None = None
    fallback_structured_outputs: bool = False

    @model_validator(mode="after")
    def require_enabled_controls(self) -> Self:
        if self.enabled and (
            not self.provider_approved
            or not self.model
            or self.model not in self.allowed_models
            or any(
                getattr(self, key) is None
                for key in (
                    "per_call_ceiling_usd",
                    "budget_usd",
                    "input_tokens",
                    "excerpt_tokens",
                    "excerpt_chars",
                    "input_bytes",
                    "timeout_seconds",
                    "retries",
                    "stage_retries",
                    "backoff_seconds",
                    "concurrency",
                )
            )
            or (self.fallback_model is not None and self.fallback_model not in self.allowed_models)
        ):
            raise ValueError("enabled classification requires approved complete bounded controls")
        if (
            self.per_call_ceiling_usd is not None
            and self.budget_usd is not None
            and self.per_call_ceiling_usd > self.budget_usd
        ):
            raise ValueError("classification ceiling exceeds budget")
        if (
            self.excerpt_tokens is not None
            and self.input_tokens is not None
            and self.excerpt_tokens >= self.input_tokens
        ):
            raise ValueError("reserve token room for instructions/options")
        return self
