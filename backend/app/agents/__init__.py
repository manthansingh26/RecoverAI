"""AI agent service layer for RecoverAI.

This package provides a provider-agnostic abstraction for LLM-powered
advisory services — root-cause diagnosis, strategy recommendation, and
decision explanation — that are consumed by the Decision Engine.

NON-NEGOTIABLE DESIGN RULE
--------------------------
The LLM is an ADVISOR ONLY. It must NEVER become the authority for
financial state transitions. The existing deterministic PolicyEngine
remains the FINAL AUTHORITY. Every agent output is validated against a
Pydantic schema before it can be consumed by any business logic.
"""

from app.agents.base import LLMProvider, LLMResult
from app.agents.diagnostician import diagnose_failure
from app.agents.recommender import recommend_strategy_for_diagnosis
from app.agents.explainer import explain_decision
from app.agents.schemas import Diagnosis, Recommendation

__all__ = [
    "LLMProvider",
    "LLMResult",
    "Diagnosis",
    "Recommendation",
    "diagnose_failure",
    "recommend_strategy_for_diagnosis",
    "explain_decision",
]