"""
chat — GreenOps grounded query interface.

Answers operational and historical questions about GreenOps optimization
activity using **retrieved project data only**:

  * decision history      — chat.history.DecisionHistoryStore (append-only JSONL)
  * monitoring time-series — monitoring.PrometheusClient range queries
  * carbon context        — the environmental snapshot captured on each decision

There is no LLM in this package. Answers are composed deterministically from
retrieved records; every figure in an answer is backed by a chat.models.Evidence
entry. When a source has no data for the requested window the answer says so
explicitly and carries no invented numbers.
"""

from chat.history import DecisionHistoryStore, DecisionRecord, record_lifecycle
from chat.interface import GreenOpsChat
from chat.models import Evidence, GroundedAnswer, QueryIntent, TimeRange
from chat.retriever import HistoryRetriever, MetricRetriever
from chat.timeparse import InvalidDateRangeError, parse_time_range

__all__ = [
    "DecisionHistoryStore",
    "DecisionRecord",
    "Evidence",
    "GreenOpsChat",
    "GroundedAnswer",
    "HistoryRetriever",
    "InvalidDateRangeError",
    "MetricRetriever",
    "QueryIntent",
    "TimeRange",
    "parse_time_range",
    "record_lifecycle",
]
