"""Ordinary mutable configuration, shared by UI, model tools and scripts."""

from datetime import datetime
from pydantic import Field, field_validator
from .contracts import Contract


class ReadAutonomy(Contract):
    pass


class ConfigureAutonomy(Contract):
    enabled: bool | None = None
    interval_seconds: int | None = Field(default=None, ge=1, le=31536000)
    next_wake_in_seconds: int | None = Field(default=None, ge=1, le=31536000)
    next_wake_at: datetime | None = None
    duties_file: str | None = Field(default=None, min_length=1, max_length=300)
    duties_content: str | None = Field(default=None, max_length=20000)
    persona_content: str | None = Field(default=None, max_length=12000)

    @field_validator("next_wake_at")
    @classmethod
    def aware(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("next_wake_at requires a timezone")
        return value
