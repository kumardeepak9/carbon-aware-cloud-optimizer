"""GreenOps chat interface — deterministic, retrieval-grounded Q&A.

``GreenOpsChat.ask(question)`` classifies the question, resolves a time window,
retrieves the relevant records / series, and composes an answer **only** from
what was retrieved. It never calls a language model and never fills a gap with a
plausible-looking value: missing data produces an explicit "I don't have that".
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from chat.history import DecisionRecord
from chat.models import Evidence, GroundedAnswer, QueryIntent, TimeRange
from chat.retriever import HistoryRetriever, HistorySlice, MetricRetriever, MetricWindow
from chat.timeparse import InvalidDateRangeError, parse_time_range

_CAPABILITIES = (
    "I can answer questions about: decisions the agent made in a period, "
    "why it scaled the workload down, the carbon intensity recorded with a "
    "decision, whether latency changed after an optimization, and which "
    "recommendations the safety policy rejected."
)


def _ts(dt_ts: float) -> str:
    return datetime.fromtimestamp(dt_ts, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def _num(value: float | None, fmt: str = "{:.0f}") -> str:
    return "unknown" if value is None else fmt.format(value)


def _secs(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.3f}s"


class GreenOpsChat:
    """Answers operational/historical questions from stored project data."""

    def __init__(
        self,
        history: HistoryRetriever,
        metrics: MetricRetriever | None = None,
        *,
        now: datetime | None = None,
        default_days: int = 7,
    ) -> None:
        self._history = history
        self._metrics = metrics
        self._now = now
        self._default_days = default_days

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def ask(self, question: str) -> GroundedAnswer:
        q = (question or "").strip()
        if not q:
            return GroundedAnswer(
                text="Ask me something. " + _CAPABILITIES,
                intent=QueryIntent.UNKNOWN,
                data_complete=False,
                unanswered_reason="empty question",
            )

        intent = self._classify(q)
        if intent is QueryIntent.UNKNOWN:
            return GroundedAnswer(
                text="I can't map that question to GreenOps data. " + _CAPABILITIES,
                intent=intent,
                data_complete=False,
                unanswered_reason="unrecognized question",
            )

        now = self._now or datetime.now(UTC)
        try:
            time_range = parse_time_range(q, now=now, default_days=self._default_days)
        except InvalidDateRangeError as exc:
            return GroundedAnswer(
                text=f"I couldn't resolve the time period in that question: {exc}.",
                intent=intent,
                data_complete=False,
                unanswered_reason=f"invalid date range: {exc}",
            )

        handler = {
            QueryIntent.DECISIONS_IN_RANGE: self._answer_decisions,
            QueryIntent.WHY_SCALED_DOWN: self._answer_why_scaled_down,
            QueryIntent.CARBON_AT_TIME: self._answer_carbon,
            QueryIntent.LATENCY_AFTER_OPTIMIZATION: self._answer_latency,
            QueryIntent.REJECTED_BY_POLICY: self._answer_rejected,
        }[intent]
        return await handler(time_range)

    # ------------------------------------------------------------------
    # Intent classification (ordered; first match wins)
    # ------------------------------------------------------------------

    @staticmethod
    def _classify(q: str) -> QueryIntent:
        s = q.lower()

        if re.search(r"\breject|\brejected|\bblocked by|\bpolicy (?:block|refus|den|reject)|"
                     r"not approved|require[sd]? review|denied", s):
            return QueryIntent.REJECTED_BY_POLICY

        scaled_down = re.search(r"scale[ -]?down|scaled[ -]?down|downscale|reduce.*replica|"
                                r"scal\w+ .*\bdown\b|fewer replica", s)
        if scaled_down and ("why" in s or "reason" in s or "explain" in s or "because" in s):
            return QueryIntent.WHY_SCALED_DOWN

        if re.search(r"latency|p99|p50|response time|slower|tail latency", s) and re.search(
            r"after|following|post|increase|impact|regress|change|worse|did .* go up", s
        ):
            return QueryIntent.LATENCY_AFTER_OPTIMIZATION

        if "carbon" in s or "gco2" in s or "grid intensity" in s or "emissions intensity" in s:
            return QueryIntent.CARBON_AT_TIME

        if re.search(r"\bdecisions?\b|\brecommendations?\b|what did you (?:do|decide)|"
                     r"what actions|what happened|optimi[sz]ation|\bactivity\b|\bcycles?\b", s):
            return QueryIntent.DECISIONS_IN_RANGE

        if scaled_down:  # "why" was implicit
            return QueryIntent.WHY_SCALED_DOWN

        return QueryIntent.UNKNOWN

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _no_history(self, intent: QueryIntent, tr: TimeRange, sl: HistorySlice) -> GroundedAnswer | None:
        """Shared guard: store missing entirely."""
        if not sl.store_exists:
            return GroundedAnswer(
                text=(
                    "No decision history has been recorded yet — the agent has "
                    "not written any optimization cycles to the history store, "
                    f"so I have nothing to report for {tr.label or 'that period'}."
                ),
                intent=intent,
                time_range=tr,
                data_complete=False,
                unanswered_reason="no decision history",
            )
        return None

    async def _answer_decisions(self, tr: TimeRange) -> GroundedAnswer:
        sl = self._history.slice(tr)
        if (guard := self._no_history(QueryIntent.DECISIONS_IN_RANGE, tr, sl)) is not None:
            return guard

        if not sl.records:
            return GroundedAnswer(
                text=(
                    f"I have decision history, but no optimization cycles are "
                    f"recorded for {tr.label} "
                    f"({tr.start.date().isoformat()} to {tr.end.date().isoformat()})."
                ),
                intent=QueryIntent.DECISIONS_IN_RANGE,
                time_range=tr,
                data_complete=sl.complete,
            )

        by_action: dict[str, int] = {}
        lines: list[str] = []
        evidence: list[Evidence] = []
        for r in sl.records:
            by_action[r.action or "UNKNOWN"] = by_action.get(r.action or "UNKNOWN", 0) + 1
            delta = ""
            if r.current_replicas is not None and r.recommended_replicas is not None:
                delta = f" ({r.current_replicas}→{r.recommended_replicas} replicas)"
            outcome = r.final_outcome or r.policy_status or "n/a"
            lines.append(
                f"- {_ts(r.started_at)}: {r.action or 'UNKNOWN'}{delta} — "
                f"policy={r.policy_status or 'n/a'}, outcome={outcome}. "
                f"Reason: {r.reason or '(none recorded)'}"
            )
            evidence.append(_record_evidence(r))

        summary = ", ".join(f"{v}×{k}" for k, v in sorted(by_action.items()))
        text = (
            f"{len(sl.records)} optimization cycle(s) recorded in {tr.label} "
            f"({summary}):\n" + "\n".join(lines)
        )
        note = "" if sl.complete else _incomplete_note(sl)
        return GroundedAnswer(
            text=text + note,
            intent=QueryIntent.DECISIONS_IN_RANGE,
            evidence=evidence,
            time_range=tr,
            data_complete=sl.complete,
        )

    async def _answer_why_scaled_down(self, tr: TimeRange) -> GroundedAnswer:
        sl = self._history.slice(tr)
        if (guard := self._no_history(QueryIntent.WHY_SCALED_DOWN, tr, sl)) is not None:
            return guard

        downs = sl.scale_downs()
        if not downs:
            return GroundedAnswer(
                text=(
                    f"No scale-down decisions are recorded in {tr.label}. "
                    f"({len(sl.records)} cycle(s) in that window; actions: "
                    f"{_action_tally(sl)}.)"
                ),
                intent=QueryIntent.WHY_SCALED_DOWN,
                time_range=tr,
                data_complete=sl.complete,
                evidence=[_record_evidence(r) for r in sl.records],
            )

        # "Why did you scale down" means an actual scale-down — prefer one the
        # policy approved / that reached GitOps. Fall back to explaining a
        # blocked attempt only if that is all the history holds.
        effective = [d for d in downs if d.approved_for_gitops_change or d.was_applied]
        r = (effective or downs)[-1]
        carbon = (
            f"{r.carbon_intensity_gco2_kwh:.0f} gCO2eq/kWh"
            if r.carbon_intensity_gco2_kwh is not None
            else "not recorded"
        )
        region = f" (region {r.carbon_region})" if r.carbon_region else ""
        renewable = (
            f"{r.renewable_percentage:.0f}% renewable"
            if r.renewable_percentage is not None
            else "renewable share not recorded"
        )
        extra = ""
        if len(downs) > 1:
            extra = f" ({len(downs)} scale-down recommendation(s) in {tr.label}; this is the most recent"
            extra += " approved one.)" if effective else ", none approved.)"
        if not effective:
            extra += (
                f"\nNote: this recommendation was {r.policy_status or 'not applied'} and did "
                "not change the workload."
            )

        text = (
            f"On {_ts(r.started_at)} the agent recommended SCALE_DOWN "
            f"{_num(r.current_replicas)}→{_num(r.recommended_replicas)} replicas.{extra}\n"
            f"Reason (verbatim): \"{r.reason}\"\n"
            f"Decision basis: {r.decision_basis or 'n/a'}; "
            f"confidence {_num(r.confidence, '{:.2f}')}.\n"
            f"Carbon intensity at decision time: {carbon}{region}, {renewable}.\n"
            f"Workload at decision time: CPU/request ratio "
            f"{_num(r.obs_cpu_request_ratio, '{:.2f}')}, p99 latency "
            f"{_secs(r.obs_p99_latency_seconds)}, availability "
            f"{_num(r.obs_availability_ratio, '{:.2f}')}.\n"
            f"Policy verdict: {r.policy_status or 'n/a'}"
            + (f" — {r.policy_reason}" if r.policy_reason else "")
            + (f"\nFinal outcome: {r.final_outcome}" if r.final_outcome else "")
        )
        missing = r.carbon_intensity_gco2_kwh is None or r.carbon_data_available is False
        return GroundedAnswer(
            text=text + ("" if sl.complete else _incomplete_note(sl)),
            intent=QueryIntent.WHY_SCALED_DOWN,
            evidence=[_record_evidence(r)],
            time_range=tr,
            data_complete=sl.complete and not missing,
        )

    async def _answer_carbon(self, tr: TimeRange) -> GroundedAnswer:
        sl = self._history.slice(tr)
        # Prefer the carbon value captured with an actual decision in the window.
        anchored = [r for r in sl.records if r.carbon_intensity_gco2_kwh is not None]
        evidence: list[Evidence] = []

        if anchored:
            lines = []
            for r in anchored:
                avail = "" if r.carbon_data_available is not False else " (agent flagged data_available=false)"
                lines.append(
                    f"- {_ts(r.started_at)} [{r.action}]: "
                    f"{r.carbon_intensity_gco2_kwh:.0f} gCO2eq/kWh"
                    + (f", {r.renewable_percentage:.0f}% renewable" if r.renewable_percentage is not None else "")
                    + (f", region {r.carbon_region}" if r.carbon_region else "")
                    + avail
                )
                evidence.append(_record_evidence(r, source="carbon_context"))
            text = (
                f"Carbon intensity recorded with decisions in {tr.label}:\n"
                + "\n".join(lines)
            )
            # Optionally corroborate with Prometheus.
            if self._metrics is not None:
                mw = await self._metrics.carbon_intensity(tr)
                if mw.available and mw.mean is not None:
                    text += (
                        f"\nPrometheus carbon series over the same window: "
                        f"mean {mw.mean:.0f}, min {mw.minimum:.0f}, max {mw.maximum:.0f} gCO2eq/kWh."
                    )
                    evidence.append(_metric_evidence(mw))
            return GroundedAnswer(
                text=text, intent=QueryIntent.CARBON_AT_TIME, evidence=evidence,
                time_range=tr, data_complete=sl.complete,
            )

        # No decision carried a carbon value — fall back to Prometheus for the range.
        if self._metrics is None:
            base = "No decision in that window recorded a carbon-intensity value"
            if not sl.store_exists:
                base = "No decision history has been recorded"
            return GroundedAnswer(
                text=(
                    f"{base}, and the metrics backend is not configured for this "
                    f"chat session, so I cannot report the carbon intensity for {tr.label}."
                ),
                intent=QueryIntent.CARBON_AT_TIME,
                time_range=tr,
                data_complete=False,
                unanswered_reason="no carbon data available",
            )

        mw = await self._metrics.carbon_intensity(tr)
        if not mw.available:
            return GroundedAnswer(
                text=(
                    f"Carbon-intensity history for {tr.label} is unavailable — "
                    "the metrics backend did not return any data for that window. "
                    "I won't estimate it."
                ),
                intent=QueryIntent.CARBON_AT_TIME,
                time_range=tr,
                data_complete=False,
                unanswered_reason=f"carbon metric unavailable: {mw.reason}",
            )
        return GroundedAnswer(
            text=(
                f"Grid carbon intensity over {tr.label} (Prometheus): "
                f"mean {mw.mean:.0f}, min {mw.minimum:.0f}, max {mw.maximum:.0f} gCO2eq/kWh "
                f"across {len(mw.points)} samples."
            ),
            intent=QueryIntent.CARBON_AT_TIME,
            evidence=[_metric_evidence(mw)],
            time_range=tr,
            data_complete=True,
        )

    async def _answer_latency(self, tr: TimeRange) -> GroundedAnswer:
        sl = self._history.slice(tr)
        if (guard := self._no_history(QueryIntent.LATENCY_AFTER_OPTIMIZATION, tr, sl)) is not None:
            return guard

        applied = [r for r in sl.applied() if r.action in {"SCALE_DOWN", "SCALE_UP"}]
        if not applied:
            return GroundedAnswer(
                text=(
                    f"No optimization was applied to the workload in {tr.label}, "
                    f"so there is no before/after latency to compare. "
                    f"(Cycles in window: {_action_tally(sl)}.)"
                ),
                intent=QueryIntent.LATENCY_AFTER_OPTIMIZATION,
                time_range=tr,
                data_complete=sl.complete,
                evidence=[_record_evidence(r) for r in sl.records],
            )

        r = applied[-1]
        pre = _get(r.pre_snapshot, "http_p99_latency_seconds")
        post = _get(r.post_snapshot, "http_p99_latency_seconds")
        evidence = [_record_evidence(r)]

        if pre is None or post is None:
            reason = r.verification_outcome or r.final_outcome or "not captured"
            return GroundedAnswer(
                text=(
                    f"The {r.action} on {_ts(r.started_at)} was applied, but a "
                    f"before/after p99 latency pair was not both captured "
                    f"(pre={_num(pre, '{:.3f}')}s, post={_num(post, '{:.3f}')}s; "
                    f"verification: {reason}). I won't guess the missing value."
                ),
                intent=QueryIntent.LATENCY_AFTER_OPTIMIZATION,
                time_range=tr,
                data_complete=False,
                evidence=evidence,
            )

        delta = post - pre
        pct = (delta / pre * 100.0) if pre else None
        direction = "increased" if delta > 0 else "decreased" if delta < 0 else "did not change"
        text = (
            f"After the {r.action} on {_ts(r.started_at)}, p99 latency {direction}: "
            f"{pre:.3f}s → {post:.3f}s "
            + (f"({pct:+.1f}%)." if pct is not None else "(baseline was 0).")
            + f"\nVerification outcome: {r.verification_outcome or 'n/a'}"
            + (f" — {r.verification_reason}" if r.verification_reason else "")
        )
        if r.rollback_prepared:
            text += "\nA rollback was prepared for this change."

        # Corroborate against Prometheus around the decision time if we can.
        if self._metrics is not None:
            mw = await self._metrics.around(
                "http_p99_latency_seconds", r.started_at, before_s=1800, after_s=3600
            )
            if mw.available and mw.first and mw.last:
                text += (
                    f"\nPrometheus p99 around that time: {mw.first[1]:.3f}s → {mw.last[1]:.3f}s."
                )
                evidence.append(_metric_evidence(mw))

        return GroundedAnswer(
            text=text,
            intent=QueryIntent.LATENCY_AFTER_OPTIMIZATION,
            evidence=evidence,
            time_range=tr,
            data_complete=sl.complete,
        )

    async def _answer_rejected(self, tr: TimeRange) -> GroundedAnswer:
        sl = self._history.slice(tr)
        if (guard := self._no_history(QueryIntent.REJECTED_BY_POLICY, tr, sl)) is not None:
            return guard

        rejected = sl.rejected()
        review = sl.require_review()
        if not rejected and not review:
            return GroundedAnswer(
                text=(
                    f"No recommendations were rejected or held for review by the "
                    f"safety policy in {tr.label}. ({len(sl.records)} cycle(s); "
                    f"actions: {_action_tally(sl)}.)"
                ),
                intent=QueryIntent.REJECTED_BY_POLICY,
                time_range=tr,
                data_complete=sl.complete,
                evidence=[_record_evidence(r) for r in sl.records],
            )

        parts: list[str] = []
        evidence: list[Evidence] = []
        if rejected:
            parts.append(f"{len(rejected)} recommendation(s) REJECTED by policy:")
            for r in rejected:
                parts.append(
                    f"- {_ts(r.started_at)}: attempted {r.action} "
                    f"{_num(r.current_replicas)}→{_num(r.recommended_replicas)}. "
                    f"Policy reason: \"{r.policy_reason or '(none)'}\". "
                    f"Safeguards: {', '.join(r.safeguards_triggered) or 'none listed'}."
                )
                evidence.append(_record_evidence(r))
        if review:
            parts.append(f"{len(review)} recommendation(s) held for REQUIRE_REVIEW:")
            for r in review:
                parts.append(
                    f"- {_ts(r.started_at)}: {r.action}. Reason: \"{r.policy_reason or '(none)'}\"."
                )
                evidence.append(_record_evidence(r))

        return GroundedAnswer(
            text="\n".join(parts) + ("" if sl.complete else _incomplete_note(sl)),
            intent=QueryIntent.REJECTED_BY_POLICY,
            evidence=evidence,
            time_range=tr,
            data_complete=sl.complete,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get(d: object, key: str) -> float | None:
    v = d.get(key) if isinstance(d, dict) else None
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _action_tally(sl: HistorySlice) -> str:
    tally: dict[str, int] = {}
    for r in sl.records:
        tally[r.action or "UNKNOWN"] = tally.get(r.action or "UNKNOWN", 0) + 1
    return ", ".join(f"{v}×{k}" for k, v in sorted(tally.items())) or "none"


def _incomplete_note(sl: HistorySlice) -> str:
    if not sl.store_exists:
        return "\n\n(Note: no history store found.)"
    if sl.skipped_lines:
        return (
            f"\n\n(Note: {sl.skipped_lines} history record(s) were unreadable and "
            "excluded — this answer may be incomplete.)"
        )
    return ""


def _record_evidence(r: DecisionRecord, source: str = "decision_history") -> Evidence:
    return Evidence(
        source=source,
        ref=r.lifecycle_id,
        timestamp=r.started_at,
        detail={
            "action": r.action,
            "reason": r.reason,
            "decision_basis": r.decision_basis,
            "confidence": r.confidence,
            "policy_status": r.policy_status,
            "policy_reason": r.policy_reason,
            "safeguards_triggered": r.safeguards_triggered,
            "current_replicas": r.current_replicas,
            "recommended_replicas": r.recommended_replicas,
            "carbon_intensity_gco2_kwh": r.carbon_intensity_gco2_kwh,
            "carbon_region": r.carbon_region,
            "renewable_percentage": r.renewable_percentage,
            "carbon_data_available": r.carbon_data_available,
            "obs_cpu_request_ratio": r.obs_cpu_request_ratio,
            "obs_p99_latency_seconds": r.obs_p99_latency_seconds,
            "obs_error_rate_rps": r.obs_error_rate_rps,
            "obs_availability_ratio": r.obs_availability_ratio,
            "final_outcome": r.final_outcome,
            "verification_outcome": r.verification_outcome,
            "pre_p99_latency_seconds": _get(r.pre_snapshot, "http_p99_latency_seconds"),
            "post_p99_latency_seconds": _get(r.post_snapshot, "http_p99_latency_seconds"),
        },
    )


def _metric_evidence(mw: MetricWindow) -> Evidence:
    return Evidence(
        source="prometheus",
        ref=mw.query,
        detail={
            "metric": mw.metric,
            "sample_count": len(mw.points),
            "first": mw.first,
            "last": mw.last,
            "mean": mw.mean,
            "min": mw.minimum,
            "max": mw.maximum,
        },
    )
