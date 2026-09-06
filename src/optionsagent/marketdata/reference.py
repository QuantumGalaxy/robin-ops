"""Validated reference-feed snapshot; metadata is supplied by a trusted data vendor."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator


class ReferenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Session(ReferenceModel):
    open: datetime
    close: datetime

    @model_validator(mode="after")
    def valid(self):
        if self.open.tzinfo is None or self.close.tzinfo is None:
            raise ValueError("Session timestamps need a timezone")
        if not 0 < (self.close - self.open).total_seconds() <= 86400:
            raise ValueError("Invalid session duration")
        return self


class ReferenceSymbol(ReferenceModel):
    daily_closes: list[float] = Field(default_factory=list)
    daily_closes_as_of: date | None = None
    iv_rank: float | None = Field(default=None, ge=0, le=1, strict=True)
    iv_history_days: StrictInt = Field(default=0, ge=0)
    earnings_checked: StrictBool = False
    earnings: date | None = None

    @model_validator(mode="after")
    def valid(self):
        if self.earnings_checked and "earnings" not in self.model_fields_set:
            raise ValueError("Explicit earnings date or null is required when checked")
        if any(x <= 0 for x in self.daily_closes):
            raise ValueError("Daily closes must be positive")
        if self.daily_closes and self.daily_closes_as_of is None:
            raise ValueError("Daily bars require their last completed session date")
        return self


class ReferenceSnapshot(ReferenceModel):
    as_of: datetime
    source: str = Field(min_length=1)
    session: Session | None = None
    symbols: dict[str, ReferenceSymbol] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid(self):
        if self.as_of.tzinfo is None or not self.source.strip():
            raise ValueError("Source and timestamp are required")
        return self
