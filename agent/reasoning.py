"""Optional explanation boundary for future LLM use.

The deterministic policy remains the sole authority for action and replica
targets.  An implementation of this protocol may only enrich the explanation
of an already-created recommendation.
"""

from __future__ import annotations

from typing import Protocol

from agent.models import DecisionRecommendation


class RecommendationExplainer(Protocol):
    """Produces human-facing explanation text without changing a decision."""

    def explain(self, recommendation: DecisionRecommendation) -> str:
        """Explain a deterministic recommendation; do not alter its action or target."""
