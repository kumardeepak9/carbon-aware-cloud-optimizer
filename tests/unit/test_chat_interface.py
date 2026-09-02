"""
tests/unit/test_chat_interface.py

Validates the GreenOps chat/query interface:

  * grounded responses      — the five sample questions answered only from
    stored decision records / retrieved metrics, with matching evidence
  * missing history         — empty store and empty windows are reported, not guessed
  * invalid date ranges     — inverted / malformed / future ranges are refused
  * unavailable metrics     — Prometheus errors and absent snapshots surface as
    "I don't have that", never a fabricated number
  * anti-fabrication        — no answer states a figure that is not in its evidence

No language model and no live Prometheus: history is a temp JSONL file, metrics
are mocked with respx.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
import respx
from httpx import Response

from chat.history import DecisionHistoryStore, DecisionRecord, record_lifecycle
from chat.interface import GreenOpsChat
from chat.models import QueryIntent
from chat.retriever import HistoryRetriever, MetricRetriever
from chat.timeparse import InvalidDateRangeError, parse_time_range

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)
PROM = "http://prom-test:9090"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> DecisionHistoryStore:
    return DecisionHistoryStore(tmp_path / "history" / "decision-history.jsonl")


def _rec(**over) -> DecisionRecord:
    base = {
        "lifecycle_id": "lc-scale-down-1",
        "started_at": (NOW - timedelta(days=3)).timestamp(),
        "completed_at": (NOW - timedelta(days=3)).timestamp() + 180,
        "action": "SCALE_DOWN",
        "reason": "Low workload demand during high grid carbon intensity; a one-replica reduction is safe to consider.",
        "decision_basis": "low_load_high_carbon",
        "confidence": 0.95,
        "current_replicas": 3,
        "recommended_replicas": 2,
        "policy_status": "APPROVED",
        "policy_reason": "within all safeguards",
        "approved_for_gitops_change": True,
        "carbon_intensity_gco2_kwh": 417.0,
        "carbon_region": "DE",
        "renewable_percentage": 19.0,
        "carbon_data_available": True,
        "obs_cpu_request_ratio": 0.14,
        "obs_p99_latency_seconds": 0.121,
        "obs_availability_ratio": 1.0,
        "pre_snapshot": {"http_p99_latency_seconds": 0.121, "replica_count_desired": 3},
        "post_snapshot": {"http_p99_latency_seconds": 0.138, "replica_count_desired": 2},
        "gitops_status": "PR_CREATED",
        "verification_outcome": "SUCCESS",
        "final_outcome": "SUCCESS",
    }
    base.update(over)
    return DecisionRecord(**base)


@pytest.fixture
def populated(store) -> DecisionHistoryStore:
    store.append(_rec())
    store.append(
        _rec(
            lifecycle_id="lc-rejected-1",
            started_at=(NOW - timedelta(days=2)).timestamp(),
            action="SCALE_DOWN",
            reason="Aggressive reduction proposed.",
            current_replicas=2,
            recommended_replicas=1,
            policy_status="REJECTED",
            policy_reason="scale-down of 50% exceeds max_scale_down_percentage (33%)",
            safeguards_triggered=["max_scale_down_percentage"],
            approved_for_gitops_change=False,
            carbon_intensity_gco2_kwh=395.0,
            gitops_status=None,
            verification_outcome=None,
            final_outcome="BLOCKED:REJECTED",
            pre_snapshot={},
            post_snapshot={},
        )
    )
    store.append(
        _rec(
            lifecycle_id="lc-keep-1",
            started_at=(NOW - timedelta(days=1)).timestamp(),
            action="KEEP",
            reason="No reliability pressure or safe reduction opportunity.",
            decision_basis="steady_state",
            current_replicas=2,
            recommended_replicas=None,
            policy_status="APPROVED",
            carbon_intensity_gco2_kwh=110.0,
            gitops_status=None,
            verification_outcome=None,
            final_outcome="NO_ACTION",
            pre_snapshot={},
            post_snapshot={},
        )
    )
    return store


def chat(store: DecisionHistoryStore, metrics: MetricRetriever | None = None) -> GreenOpsChat:
    return GreenOpsChat(HistoryRetriever(store), metrics, now=NOW)


def _numbers(text: str) -> set[str]:
    """All numeric tokens in a string (for anti-fabrication checks)."""
    return set(re.findall(r"\d+(?:\.\d+)?", text))


async def _ask(store, q, metrics=None):
    return await chat(store, metrics).ask(q)


# ---------------------------------------------------------------------------
# 1. Time-range parsing
# ---------------------------------------------------------------------------


class TestTimeParsing:
    @pytest.mark.parametrize(
        "text,label",
        [
            ("what happened last week?", "last week"),
            ("decisions in the last 3 days", "last 3 days"),
            ("what did you do yesterday", "yesterday"),
            ("activity this month", "this month"),
            ("from 2026-08-01 to 2026-08-07", "2026-08-01 to 2026-08-07"),
            ("on 2026-08-20", "2026-08-20"),
            ("since 2026-08-15", "since 2026-08-15"),
        ],
    )
    def test_recognised_windows(self, text, label):
        tr = parse_time_range(text, now=NOW)
        assert tr.label == label
        assert tr.start < tr.end <= NOW

    def test_no_period_uses_default(self):
        tr = parse_time_range("what decisions did you make?", now=NOW, default_days=7)
        assert (NOW - tr.start).days == 7

    def test_no_period_can_be_required(self):
        with pytest.raises(InvalidDateRangeError):
            parse_time_range("what decisions did you make?", now=NOW, default_days=None)

    @pytest.mark.parametrize(
        "text",
        [
            "from 2026-08-10 to 2026-08-01",   # inverted
            "between 2020-01-01 and 2019-01-01",
            "on 2026-13-40",                    # not a real date
            "in the last 0 days",               # non-positive
            "from 2099-01-01 to 2099-03-01",    # entirely future
            "since 1900-01-01",                 # absurdly far back
        ],
    )
    def test_invalid_ranges_raise(self, text):
        with pytest.raises(InvalidDateRangeError):
            parse_time_range(text, now=NOW)


# ---------------------------------------------------------------------------
# 2. Grounded responses to the five sample questions
# ---------------------------------------------------------------------------


class TestGroundedResponses:
    @pytest.mark.asyncio
    async def test_what_decisions_last_week(self, populated):
        a = await _ask(populated, "What decisions did you make last week?")
        assert a.intent is QueryIntent.DECISIONS_IN_RANGE
        assert a.answered and a.data_complete
        # exactly the three stored cycles, each cited
        assert len(a.evidence) == 3
        assert {e.ref for e in a.evidence} == {"lc-scale-down-1", "lc-rejected-1", "lc-keep-1"}
        assert "SCALE_DOWN" in a.text and "KEEP" in a.text
        assert "3 optimization cycle" in a.text

    @pytest.mark.asyncio
    async def test_why_scale_down(self, populated):
        a = await _ask(populated, "Why did you scale down the workload?")
        assert a.intent is QueryIntent.WHY_SCALED_DOWN
        # verbatim recorded reason, not a paraphrase
        assert '"Low workload demand during high grid carbon intensity' in a.text
        assert "low_load_high_carbon" in a.text
        assert a.evidence and a.evidence[0].detail["decision_basis"] == "low_load_high_carbon"

    @pytest.mark.asyncio
    async def test_carbon_intensity_at_the_time(self, populated):
        a = await _ask(populated, "What was the carbon intensity at the time?")
        assert a.intent is QueryIntent.CARBON_AT_TIME
        assert "417" in a.text  # the value stored on lc-scale-down-1
        # every intensity in the answer must be carried by a cited record
        cited_intensities = {
            e.detail["carbon_intensity_gco2_kwh"] for e in a.evidence
            if e.detail.get("carbon_intensity_gco2_kwh") is not None
        }
        assert cited_intensities == {417.0, 395.0, 110.0}

    @pytest.mark.asyncio
    async def test_latency_after_optimization(self, populated):
        a = await _ask(populated, "Did latency increase after the optimization?")
        assert a.intent is QueryIntent.LATENCY_AFTER_OPTIMIZATION
        assert "0.121" in a.text and "0.138" in a.text
        assert "increased" in a.text
        assert a.evidence[0].detail["pre_p99_latency_seconds"] == 0.121
        assert a.evidence[0].detail["post_p99_latency_seconds"] == 0.138

    @pytest.mark.asyncio
    async def test_rejected_by_policy(self, populated):
        a = await _ask(populated, "Were any recommendations rejected by policy?")
        assert a.intent is QueryIntent.REJECTED_BY_POLICY
        assert "1 recommendation(s) REJECTED" in a.text
        assert "max_scale_down_percentage" in a.text
        assert [e.ref for e in a.evidence] == ["lc-rejected-1"]

    @pytest.mark.asyncio
    async def test_answer_serialises(self, populated):
        a = await _ask(populated, "What decisions did you make last week?")
        d = a.to_dict()
        assert d["intent"] == "decisions_in_range"
        assert d["time_range"]["label"] == "last week"
        assert len(d["evidence"]) == 3


# ---------------------------------------------------------------------------
# 3. Missing history
# ---------------------------------------------------------------------------


class TestMissingHistory:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "q",
        [
            "What decisions did you make last week?",
            "Why did you scale down the workload?",
            "Were any recommendations rejected by policy?",
            "Did latency increase after the optimization?",
        ],
    )
    async def test_empty_store_is_reported_not_guessed(self, store, q):
        a = await _ask(store, q)
        assert not a.answered
        assert a.unanswered_reason == "no decision history"
        assert a.evidence == []
        assert "No decision history has been recorded" in a.text

    @pytest.mark.asyncio
    async def test_window_with_no_cycles(self, populated):
        a = await _ask(populated, "What decisions did you make on 2026-01-05?")
        assert a.answered
        assert a.evidence == []
        assert "no optimization cycles" in a.text
        # only the echoed date parts, nothing that looks like a metric
        assert _numbers(a.text) <= {"2026", "01", "05", "06"}

    @pytest.mark.asyncio
    async def test_no_scale_down_in_window(self, store):
        store.append(_rec(action="SCALE_UP", reason="load high", recommended_replicas=4))
        a = await _ask(store, "Why did you scale the workload down?")
        assert a.answered
        assert "No scale-down decisions are recorded" in a.text

    @pytest.mark.asyncio
    async def test_corrupt_line_marks_incomplete(self, store):
        store.append(_rec())
        store.path.open("a").write("{not valid json\n")
        a = await _ask(store, "What decisions did you make last week?")
        assert a.answered
        assert a.data_complete is False
        assert "unreadable" in a.text


# ---------------------------------------------------------------------------
# 4. Invalid date ranges
# ---------------------------------------------------------------------------


class TestInvalidDateRanges:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "q",
        [
            "What decisions did you make from 2026-08-10 to 2026-08-01?",
            "What did you do between 2020-01-01 and 2019-06-01?",
            "Show decisions on 2026-19-99",
            "What happened in the last 0 days?",
            "Decisions from 2099-01-01 to 2099-02-01?",
        ],
    )
    async def test_invalid_range_is_refused(self, populated, q):
        a = await _ask(populated, q)
        assert not a.answered
        assert a.unanswered_reason.startswith("invalid date range")
        assert a.evidence == []
        assert "couldn't resolve the time period" in a.text

    @pytest.mark.asyncio
    async def test_refusal_does_not_leak_data(self, populated):
        a = await _ask(populated, "decisions from 2026-08-10 to 2026-08-01")
        # must not have retrieved / quoted any record
        assert "SCALE_DOWN" not in a.text and "417" not in a.text


# ---------------------------------------------------------------------------
# 5. Unavailable metrics
# ---------------------------------------------------------------------------


class TestUnavailableMetrics:
    @pytest.mark.asyncio
    async def test_carbon_question_without_backend_or_record(self, store):
        # a decision with NO carbon value recorded
        store.append(_rec(carbon_intensity_gco2_kwh=None, carbon_data_available=False))
        a = await _ask(store, "What was the carbon intensity last week?")
        assert not a.answered
        assert a.unanswered_reason == "no carbon data available"
        assert _numbers(a.text) <= {"7"}  # only "last 7 days"

    @pytest.mark.asyncio
    async def test_latency_question_when_post_snapshot_missing(self, store):
        store.append(
            _rec(
                pre_snapshot={"http_p99_latency_seconds": 0.12},
                post_snapshot={},  # verification never captured a post value
                verification_outcome="INCONCLUSIVE",
            )
        )
        a = await _ask(store, "Did latency increase after the optimization?")
        assert a.data_complete is False
        assert "won't guess" in a.text
        assert "INCONCLUSIVE" in a.text

    @pytest.mark.asyncio
    @respx.mock
    async def test_carbon_from_prometheus_when_unreachable(self, store):
        store.append(_rec(carbon_intensity_gco2_kwh=None))
        respx.get(f"{PROM}/api/v1/query_range").mock(
            return_value=Response(503, json={"status": "error", "errorType": "unavailable", "error": "down"})
        )
        from monitoring.client import PrometheusClient

        async with PrometheusClient(base_url=PROM, max_retries=0) as client:
            a = await _ask(store, "carbon intensity last week", MetricRetriever(client))
        assert not a.answered
        assert "unavailable" in a.text.lower() and "won't estimate" in a.text.lower()
        assert _numbers(a.text) == set()  # no fabricated intensity
        assert a.unanswered_reason.startswith("carbon metric unavailable")

    @pytest.mark.asyncio
    @respx.mock
    async def test_carbon_from_prometheus_when_available(self, store):
        # history exists but records carry no carbon; Prometheus does have it
        store.append(_rec(carbon_intensity_gco2_kwh=None))
        ts = (NOW - timedelta(days=1)).timestamp()
        respx.get(f"{PROM}/api/v1/query_range").mock(
            return_value=Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "matrix",
                        "result": [{"metric": {}, "values": [[ts, "300"], [ts + 300, "360"]]}],
                    },
                },
            )
        )
        from monitoring.client import PrometheusClient

        async with PrometheusClient(base_url=PROM, max_retries=0) as client:
            a = await _ask(store, "what was the carbon intensity last week", MetricRetriever(client))
        assert a.answered and a.data_complete
        assert "330" in a.text  # mean of 300 and 360, from the mocked series
        assert a.evidence[0].source == "prometheus"


# ---------------------------------------------------------------------------
# 6. Anti-fabrication
# ---------------------------------------------------------------------------


class TestAntiFabrication:
    @pytest.mark.asyncio
    async def test_no_invented_numbers_on_empty_store(self, store):
        for q in [
            "What decisions did you make last week?",
            "Why did you scale down the workload?",
            "What was the carbon intensity at the time?",
            "Did latency increase after the optimization?",
            "Were any recommendations rejected by policy?",
        ]:
            a = await _ask(store, q)
            # only the "7" from "last 7 days" may appear; never a metric value
            assert _numbers(a.text) <= {"7"}, (q, a.text)
            assert a.grounded

    @pytest.mark.asyncio
    async def test_stated_metrics_are_carried_by_evidence(self, populated):
        """Key figures in each answer must appear in that answer's evidence."""
        a = await _ask(populated, "Why did you scale down the workload?")
        det = a.evidence[0].detail
        assert f"{det['carbon_intensity_gco2_kwh']:.0f}" in a.text
        assert f"{det['renewable_percentage']:.0f}%" in a.text
        assert f"{det['obs_p99_latency_seconds']:.3f}" in a.text
        assert f"{det['confidence']:.2f}" in a.text

        a = await _ask(populated, "Did latency increase after the optimization?")
        det = a.evidence[0].detail
        assert f"{det['pre_p99_latency_seconds']:.3f}" in a.text
        assert f"{det['post_p99_latency_seconds']:.3f}" in a.text

    @pytest.mark.asyncio
    async def test_does_not_invent_decisions(self, store):
        store.append(_rec())  # exactly one decision
        a = await _ask(store, "What decisions did you make last week?")
        assert len(a.evidence) == 1
        assert a.evidence[0].ref == "lc-scale-down-1"
        # exactly one enumerated decision line
        assert len([ln for ln in a.text.splitlines() if ln.startswith("- ")]) == 1
        assert "1 optimization cycle(s)" in a.text

    @pytest.mark.asyncio
    async def test_missing_carbon_value_is_stated_not_filled(self, store):
        store.append(_rec(carbon_intensity_gco2_kwh=None, renewable_percentage=None))
        a = await _ask(store, "Why did you scale down the workload?")
        assert "not recorded" in a.text
        assert a.data_complete is False

    @pytest.mark.asyncio
    async def test_unknown_question_is_declined(self, populated):
        a = await _ask(populated, "What's the meaning of life?")
        assert a.intent is QueryIntent.UNKNOWN
        assert not a.answered
        assert "can't map that question" in a.text.lower() or "can answer" in a.text.lower()
        assert a.evidence == []


# ---------------------------------------------------------------------------
# 7. MetricRetriever
# ---------------------------------------------------------------------------


class TestMetricRetriever:
    @pytest.mark.asyncio
    @respx.mock
    async def test_parses_matrix(self):
        from monitoring.client import PrometheusClient

        ts = NOW.timestamp()
        respx.get(f"{PROM}/api/v1/query_range").mock(
            return_value=Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "matrix",
                        "result": [{"metric": {}, "values": [[ts, "1.0"], [ts + 60, "3.0"]]}],
                    },
                },
            )
        )
        async with PrometheusClient(base_url=PROM, max_retries=0) as client:
            mr = MetricRetriever(client)
            tr = parse_time_range("last 1 hour", now=NOW)
            mw = await mr.carbon_intensity(tr)
        assert mw.available
        assert mw.mean == 2.0 and mw.minimum == 1.0 and mw.maximum == 3.0

    @pytest.mark.asyncio
    @respx.mock
    async def test_empty_series_is_unavailable(self):
        from monitoring.client import PrometheusClient

        respx.get(f"{PROM}/api/v1/query_range").mock(
            return_value=Response(
                200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}
            )
        )
        async with PrometheusClient(base_url=PROM, max_retries=0) as client:
            mw = await MetricRetriever(client).p99_latency(parse_time_range("last 1 hour", now=NOW))
        assert not mw.available
        assert mw.mean is None
        assert "no samples" in mw.reason or "empty" in mw.reason


# ---------------------------------------------------------------------------
# 8. Persistence: lifecycle -> record -> store
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_from_lifecycle_maps_fields(self):
        lifecycle = {
            "lifecycle_id": "lc-x",
            "started_at": 1000.0,
            "completed_at": 1180.0,
            "recommendation_json": {
                "action": "SCALE_DOWN",
                "reason": "carbon high, load low",
                "current_replicas": 3,
                "recommended_replicas": 2,
                "metadata": {"decision_basis": "low_load_high_carbon", "confidence": 0.95},
                "environmental_context": {
                    "carbon_intensity_gco2_kwh": 480.0,
                    "region": "DE",
                    "renewable_percentage": 12.0,
                    "data_available": True,
                },
                "operational_context": {"cpu_request_ratio": 0.1, "p99_latency_seconds": 0.2},
            },
            "validation_json": {
                "status": "APPROVED",
                "reason": "ok",
                "approved_for_gitops_change": True,
                "safeguards_triggered": [],
            },
            "pre_snapshot_json": {"http_p99_latency_seconds": 0.2},
            "post_snapshot_json": {"http_p99_latency_seconds": 0.21},
            "gitops_status": "PR_CREATED",
            "verification_outcome": "SUCCESS",
            "final_outcome": "SUCCESS",
            "rollback_prepared": False,
        }
        r = DecisionRecord.from_lifecycle(lifecycle)
        assert r.action == "SCALE_DOWN"
        assert r.carbon_intensity_gco2_kwh == 480.0
        assert r.policy_status == "APPROVED"
        assert r.was_applied and not r.was_rejected

    def test_record_lifecycle_appends(self, store):
        lc = {"lifecycle_id": "lc-1", "started_at": 1.0, "recommendation_json": {"action": "KEEP", "reason": "steady"}, "validation_json": {"status": "APPROVED"}}
        record_lifecycle(store, lc)
        record_lifecycle(store, {**lc, "lifecycle_id": "lc-2"})
        recs = store.all()
        assert [r.lifecycle_id for r in recs] == ["lc-1", "lc-2"]

    @pytest.mark.asyncio
    async def test_controller_persists_completed_cycle(self, store):
        from unittest.mock import AsyncMock, MagicMock

        from agent.controller import ClosedLoopController
        from agent.models import (
            Action,
            DecisionMetadata,
            DecisionRecommendation,
            EnvironmentalContext,
            OperationalContext,
            PolicyValidation,
            ValidatedRecommendation,
            ValidationStatus,
        )
        from monitoring.models import AgentObservation

        rec = DecisionRecommendation(
            action=Action.DEFER,
            reason="Insufficient monitoring data.",
            environmental_context=EnvironmentalContext(),
            operational_context=OperationalContext(),
            metadata=DecisionMetadata(confidence=0.0, decision_basis="missing_data"),
        )
        validated = ValidatedRecommendation(
            recommendation=rec,
            validation=PolicyValidation(
                status=ValidationStatus.REQUIRE_REVIEW,
                reason="deferred",
                evaluated_at_seconds=0.0,
            ),
        )
        agent = AsyncMock()
        agent.recommend = AsyncMock(return_value=validated)
        prom = AsyncMock()
        prom.collect_agent_observation = AsyncMock(
            return_value=AgentObservation(snapshots=[], collected_at=0.0)
        )

        controller = ClosedLoopController(
            prometheus_client=prom,
            queries=MagicMock(namespace="greenops", deployment="greenops-demo-workload"),
            decision_agent=agent,
            history_store=store,
        )
        await controller.run_optimization_cycle(sleep_for_stabilization=False)

        recs = store.all()
        assert len(recs) == 1
        assert recs[0].action == "DEFER"
        assert recs[0].final_outcome == "DEFERRED"
