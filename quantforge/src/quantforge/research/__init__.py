"""Frozen research recipes only; no OOS seal, execution or result authority."""

from .admission import admit_research_request
from .models import ExperimentSpec, ResearchAdmissionError, ResearchRequest

__all__ = [
    "ExperimentSpec",
    "ResearchAdmissionError",
    "ResearchRequest",
    "admit_research_request",
]
