"""Bounded evaluation intentions; numerical quotations remain host-resolved facts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvaluationAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    q: str = Field(pattern=r"^[A-Z][0-9]{2}$", max_length=3)
    op: Literal[
        "sma",
        "mom",
        "weighted",
        "fact",
        "insufficient",
        "refuse",
        "reuse",
        "pause",
        "same_key",
        "resume",
        "recover",
    ]
    a: str = Field(default="", max_length=40)
    c: tuple[Annotated[str, Field(min_length=1, max_length=20)], ...] = Field(
        default=(), max_length=2
    )


class EvaluationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    evidence_refs: tuple[str, ...] = Field(max_length=30)
    answers: tuple[EvaluationAnswer, ...] = Field(min_length=1, max_length=60)

    @model_validator(mode="after")
    def unique_questions(self) -> EvaluationBatch:
        if len({answer.q for answer in self.answers}) != len(self.answers):
            raise ValueError("exactly one bounded answer per question")
        return self
