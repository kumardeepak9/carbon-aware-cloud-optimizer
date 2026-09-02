# chat — GreenOps grounded query interface

Answers operational and historical questions about GreenOps optimization
activity. Every answer is composed **only from retrieved project data**:

| Source | Module | Used for |
|---|---|---|
| Decision history | `chat.history.DecisionHistoryStore` (append-only JSONL) | what the agent decided, why, policy verdicts, pre/post metrics |
| Monitoring time-series | `monitoring.PrometheusClient` range queries | carbon / latency history for a window |
| Carbon context | the environmental snapshot recorded **on each decision** | grid intensity "at the time" of a decision |

There is **no language model** in this package. Intent routing and answer prose
are deterministic. Every figure an answer states is backed by a
`chat.models.Evidence` entry that names its source record or series.

## Guarantees

- **No fabrication.** If a source has no data for the requested window, the
  answer says so explicitly (`data_complete=False`, or `unanswered_reason` set)
  and contains no invented numbers.
- **Retrieval, not memory.** Historical facts come from `DecisionHistoryStore`
  records written by the controller — never reconstructed.
- **Invalid date ranges are refused**, not silently reinterpreted: an inverted
  range, an impossible date (`2026-13-40`), or an entirely-future window returns
  `unanswered_reason="invalid date range: …"` with no retrieval.
- **Missing metrics are surfaced.** A Prometheus error or an absent pre/post
  snapshot produces "I don't have that", never an estimate.

## Questions it answers

| Question | Intent | Grounded in |
|---|---|---|
| "What decisions did you make last week?" | `decisions_in_range` | every `DecisionRecord` in the window |
| "Why did you scale down the workload?" | `why_scaled_down` | the most recent *approved* `SCALE_DOWN` record — verbatim `reason`, `decision_basis`, carbon + operational context |
| "What was the carbon intensity at the time?" | `carbon_at_time` | `carbon_intensity_gco2_kwh` captured on decisions in the window; optionally corroborated by the Prometheus carbon series |
| "Did latency increase after the optimization?" | `latency_after_optimization` | `pre_snapshot` / `post_snapshot` p99 latency of the last applied change |
| "Were any recommendations rejected by policy?" | `rejected_by_policy` | records with `policy_status` REJECTED / REQUIRE_REVIEW — verbatim `policy_reason`, `safeguards_triggered` |

An unrecognised question returns `intent=unknown` and lists what it can answer.

## Usage

```bash
# CLI
python -m chat.cli "were any recommendations rejected by policy?"
python -m chat.cli --json "what decisions did you make between 2026-08-01 and 2026-08-07?"
python -m chat.cli --no-metrics "why did you scale down the workload last week?"
```

```python
from chat import GreenOpsChat, DecisionHistoryStore, HistoryRetriever

store = DecisionHistoryStore("./reports/decision-history.jsonl")
answer = await GreenOpsChat(HistoryRetriever(store)).ask("what did you do yesterday?")
print(answer.text)
for e in answer.evidence:            # provenance for every claim
    print(e.source, e.ref, e.detail)
```

## Populating the history

`agent.controller.ClosedLoopController(..., history_store=DecisionHistoryStore(path))`
appends every completed lifecycle — including `DEFERRED` and policy-`REJECTED`
cycles — as one JSON line. Config: `REPORT_DECISION_HISTORY_PATH`.
