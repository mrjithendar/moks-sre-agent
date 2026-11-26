"""The incident flow's evidence validation (docs/crewai-sre-engine.md, section 4).

Agents propose; this module decides, in code, from the evidence registry. It parses each task's answer against its
contract, accepts findings only with recorded citations, verifies the resource a finding names against the evidence
it cites, grades every finding and hypothesis with named criteria (the model's own confidence is never read), applies
the reviewer's verdicts and conflicts, places changes in their windows, grades severity, builds the timeline, and maps
the result onto MOKS's Report. Everything is rule-based, so the same evidence and answers give the same result.

`Case` is the investigation's short-term memory: what has been decided so far. Later tasks see it as a case file
inside an untrusted-data block, because it carries words that agents wrote.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError

from ..orchestrator import evidence as ev
from ..orchestrator.report import Cause, Fact, Report, parse_json_object
from ..redaction import redact
from .incident_config import CHANGE_WINDOWS, REQUESTABLE
from .schema import (RAW, STATUS_LABEL, WINDOWS, Change, Criterion, EvidenceRequest, Finding, Hypothesis, RawFindings, RawHypotheses, RawPlan, RawRCA,
                     RawRequest, RawReview, Severity, TimelineEvent, TimeRange)

FINDING_WEIGHTS = {"live_evidence": 0.35, "resource_in_evidence": 0.25, "complete_result": 0.15, "fresh": 0.10, "not_challenged": 0.15}
HYPOTHESIS_WEIGHTS = {"live_support": 0.30, "independent_signals": 0.20, "confirmed_findings": 0.15, "no_contradiction": 0.15, "reviewer_upheld": 0.15,
                      "change_precedes": 0.05}
STATUS_ORDER = ("strongly_supported", "confirmed_observation", "possible_explanation", "insufficient_evidence")
VERDICTS = ("supported", "unsupported", "contradicted", "insufficient")
EVIDENCE_ID = re.compile(r"^E\d{1,4}$")
FINDING_ID = re.compile(r"^F\d{1,3}$")
HYPOTHESIS_ID = re.compile(r"^H\d{1,2}$")
POINT_IN_TIME = {"k8s_triage", "k8s_workloads", "k8s_list_pods", "k8s_describe_pod", "k8s_nodes", "k8s_autoscaling", "k8s_storage", "k8s_services",
                 "k8s_config_audit", "k8s_rollout_history", "k8s_argocd_status", "datastore_clusters", "pg_overview", "redis_overview", "prom_alerts", "grafana_alerts"}

# Which kind of signal a tool reads. Two hypotheses' supports are independent when they come from different kinds:
# the cluster's state, logs, metrics, changes, dependencies or documentation.
SIGNAL = {"k8s_pod_logs": "logs", "loki_search": "logs", "k8s_rollout_history": "changes", "git_recent_commits": "changes", "git_compare_deployment": "changes",
          "k8s_argocd_status": "changes", "k8s_config_audit": "configuration", "datastore_clusters": "dependencies", "pg_overview": "dependencies",
          "redis_overview": "dependencies", "kb_search": "documentation", "shared_kb_search": "documentation", "similar_incidents": "documentation", "incident_history": "documentation",
          "ml_anomalies": "model", "ml_observations": "model"}


def signal_of(tool: str) -> str:
    if tool in SIGNAL:
        return SIGNAL[tool]
    return "metrics" if tool.startswith(("prom_", "grafana_")) else "kubernetes"


def when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value or not isinstance(value, str):
        return None
    try:
        out = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


def iso(t: datetime | None) -> str | None:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


# ---- task answers ---------------------------------------------------------------------------------

def parse(task: str, text: str) -> tuple[Any | None, str | None]:
    """A task's answer as its Raw model, or None and why it does not fit the contract."""
    if not text or not text.strip():
        return None, "the answer was empty"
    try:
        obj = parse_json_object(text)
    except (ValueError, json.JSONDecodeError) as exc:
        return None, f"the answer is not one JSON object ({exc})"
    try:
        return RAW[task].model_validate(obj), None
    except ValidationError as exc:
        e = exc.errors()[0]
        return None, f"{'.'.join(map(str, e['loc']))}: {e['msg']}"


def guardrail(task: str, seen: dict[str, str]):
    """A CrewAI task guardrail: a malformed answer goes back to the agent once with the reason (output_retries).
    `seen` keeps the last answer, so an answer that never fits can still be read leniently afterwards."""
    def check(output):  # unannotated: CrewAI rejects a return annotation that is not typing.Tuple[bool, Any]
        raw = str(getattr(output, "raw", output) or "")
        seen[task] = raw
        parsed, why = parse(task, raw)
        if parsed is None:
            return False, f"MOKS could not read your answer: {why}. Answer again with only the JSON object the task asks for."
        return True, raw
    return check


# ---- the case ---------------------------------------------------------------------------------------

@dataclass
class Case:
    """What has been decided so far: plan, findings, hypotheses, review. MOKS's short-term memory of the run."""
    window: tuple[datetime, datetime]
    change_window: str = "24h"
    plan: RawPlan | None = None
    affected: list[dict[str, Any]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    verdicts: dict[str, dict[str, Any]] = field(default_factory=dict)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    requests: list[EvidenceRequest] = field(default_factory=list)
    unsupported: list[dict[str, str]] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    reviewed: bool = False
    rca: RawRCA | None = None
    severity: Severity | None = None
    onset: datetime | None = None

    # -- the case file ---------------------------------------------------------------------------
    def text(self, limit: int = 6000) -> str:
        lines: list[str] = []
        if self.severity:
            lines.append(f"Severity (graded by MOKS): {self.severity.level}: {'; '.join(self.severity.criteria[:3])}")
        if self.affected:
            lines.append("Affected: " + "; ".join(f"{a['resource']} ({', '.join(a['evidence'])})" for a in self.affected))
        if self.plan and self.plan.objectives:
            lines.append("Objectives:\n" + "\n".join(f"- {o}" for o in self.plan.objectives))
        if self.onset:
            lines.append(f"Symptom onset in the evidence: {iso(self.onset)}")
        if self.findings:
            lines.append("Findings:")
            lines += [f"{f.id} [{STATUS_LABEL[f.status]}, {f.confidence:.2f}] {f.resource or '-'}: {f.observation} ({', '.join(f.citations)})" for f in self.findings]
        if self.changes:
            lines.append(f"Changes in the last {self.change_window} (placed by MOKS):")
            lines += [f"{c.id} within {c.window}: {c.kind} {c.resource} at {c.at}: {c.detail} ({', '.join(c.evidence)})" for c in self.changes[:15]]
        if self.hypotheses:
            lines.append("Hypotheses (graded by MOKS):")
            for h in self.hypotheses:
                review = f"; reviewer: {h.review['verdict']}" if h.review else ""
                lines.append(f"{h.id} [{STATUS_LABEL[h.status]}, {h.confidence:.2f}] {h.statement} (for: {', '.join(h.supporting) or '-'}; "
                             f"against: {', '.join(h.contradicting) or '-'}{review})")
        if self.conflicts:
            lines.append("Conflicts: " + "; ".join(f"{', '.join(c['ids'])}: {c['description']}" for c in self.conflicts))
        if self.missing or self.gaps:
            lines.append("Missing telemetry and gaps: " + "; ".join(dict.fromkeys([*self.missing, *self.gaps]))[:1200])
        if self.requests:
            lines.append("Evidence MOKS collected for the review: " + "; ".join(f"{r.tool} -> {r.evidence or r.status}" for r in self.requests))
        out = "\n".join(lines) or "(nothing has been decided yet)"
        return out if len(out) <= limit else out[:limit] + "\n…[case file cut]"


# ---- evidence checks ----------------------------------------------------------------------------

def _haystack(ref: str, registry: Any, views: dict[str, Any]) -> str:
    entry = registry.records.get(ref) or {}
    view = views.get(ref)
    body = view if isinstance(view, str) else json.dumps(view, default=str) if view is not None else ""
    return f"{entry.get('excerpt', '')}\n{body}".lower()


def _name(resource: str) -> str:
    return resource.strip().rsplit("/", 1)[-1].lower()


def _time_range(entry: dict[str, Any], window: tuple[datetime, datetime]) -> TimeRange:
    collected = when(entry.get("timestamp"))
    args = entry.get("query") or {}
    if entry.get("source") in POINT_IN_TIME:
        return TimeRange(start=iso(collected), end=iso(collected), point_in_time=True)
    end = min(collected or window[1], window[1])
    if isinstance(args.get("minutes"), int):
        return TimeRange(start=iso(max(window[0] - timedelta(days=7), end - timedelta(minutes=args["minutes"]))), end=iso(end))
    if isinstance(args.get("hours"), int):
        return TimeRange(start=iso(end - timedelta(hours=args["hours"])), end=iso(end))
    return TimeRange(start=iso(window[0]), end=iso(window[1]))


def _live(entry: dict[str, Any]) -> bool:
    return bool(entry.get("ok")) and entry.get("kind") in ev.LIVE


def _documentation(entry: dict[str, Any]) -> bool:
    """A lead: documentation, an earlier observation, or what an ML model inferred. It can make something a possible
    explanation, never a confirmed observation or a strongly supported hypothesis: those need live reads."""
    return bool(entry.get("ok")) and entry.get("kind") in (ev.DOCUMENTATION, ev.HISTORICAL, ev.MODEL_OUTPUT)


def _score(criteria: list[Criterion]) -> float:
    return round(sum(c.weight for c in criteria if c.met), 2)


def accept_findings(case: Case, task: str, agent: str, raw: RawFindings, registry: Any, views: dict[str, Any]) -> int:
    """The specialist's findings that cite recorded evidence, numbered by MOKS. A finding with no valid citation is
    listed as unsupported; one that cites only failed reads is a gap, not a finding. Returns how many were accepted."""
    accepted = 0
    for item in raw.findings:
        if not item.observation:
            continue
        refs = [r for r in dict.fromkeys(item.evidence) if EVIDENCE_ID.match(r)]
        bad = [r for r in item.evidence if r not in registry.records]
        case.rejected += [r for r in bad if r not in case.rejected]
        refs = [r for r in refs if r in registry.records]
        if not refs:
            case.unsupported.append({"statement": item.observation[:200], "task": task, "reason": "cites no recorded evidence"})
            continue
        entries = [registry.records[r] for r in refs]
        if not any(e.get("ok") for e in entries):
            case.gaps.append(f"{task}: {item.observation[:200]} (only failed reads: {', '.join(refs)})")
            continue
        newest = max((when(e.get("timestamp")) for e in entries), default=None, key=lambda t: t or datetime.min.replace(tzinfo=timezone.utc))
        at = when(item.at)
        event_time = iso(at) if at and any(iso(at)[:16].lower() in _haystack(r, registry, views) for r in refs) else None  # minute precision, as the evidence wrote it
        first_live = next((e for e in entries if _live(e)), entries[0])
        case.findings.append(Finding(id=f"F{len(case.findings) + 1}", observation=item.observation, task=task, agent=agent, resource=item.resource,
                                     resource_verified=False, evidence_source=sorted({e["source"] for e in entries}), citations=refs, timestamp=iso(newest),
                                     event_time=event_time, time_range=_time_range(first_live, case.window), confidence=0.0, criteria=[], status="insufficient_evidence"))
        accepted += 1
    case.gaps += [f"{task}: {g}" for g in raw.gaps]
    return accepted


def _resolve(ids: list[str], case: Case, registry: Any) -> tuple[list[str], list[str]]:
    """Cited ids as (evidence ids, finding ids): a finding stands for its own citations. Unknown ids are rejected."""
    refs: list[str] = []
    found: list[str] = []
    by_id = {f.id: f for f in case.findings}
    for i in ids:
        if FINDING_ID.match(i) and i in by_id:
            found.append(i)
            refs += [r for r in by_id[i].citations if r not in refs]
        elif EVIDENCE_ID.match(i) and i in registry.records:
            refs += [i] if i not in refs else []
        elif i not in case.rejected:
            case.rejected.append(i)
    return refs, found


def accept_hypotheses(case: Case, raw: RawHypotheses, registry: Any) -> int:
    case.hypotheses = []
    for item in raw.hypotheses:
        if not item.statement:
            continue
        supporting, found = _resolve(item.supporting, case, registry)
        contradicting, _ = _resolve(item.contradicting, case, registry)
        if not supporting:
            case.unsupported.append({"statement": item.statement[:200], "task": "hypotheses", "reason": "a hypothesis that cites no recorded evidence or finding"})
            continue
        case.hypotheses.append(Hypothesis(id=f"H{len(case.hypotheses) + 1}", statement=item.statement, supporting=supporting, supporting_findings=found,
                                          contradicting=[r for r in contradicting if r not in supporting], would_confirm=item.would_confirm,
                                          confidence=0.0, criteria=[], status="insufficient_evidence"))
    return len(case.hypotheses)


def apply_review(case: Case, raw: RawReview, round_: int, max_requests: int) -> list[RawRequest]:
    """The reviewer's verdicts and conflicts, on ids that exist; its missing telemetry; and the evidence requests MOKS
    may run (allowlisted tools only, at most `max_requests`, none it has already run)."""
    case.reviewed = True
    known = {f.id for f in case.findings} | {h.id for h in case.hypotheses}
    for v in raw.verdicts:
        verdict = v.verdict.lower()
        if v.id in known and verdict in VERDICTS:
            case.verdicts[v.id] = {"verdict": verdict, "reason": v.reason, "evidence": v.evidence, "round": round_}
    for c in raw.conflicts:
        ids = [i for i in c.ids if i in known]
        if len(ids) >= 2 and c.description:
            case.conflicts.append({"ids": ids, "description": c.description, "round": round_})
    case.missing += [m for m in raw.missing_telemetry if m not in case.missing]
    done = {(r.tool, json.dumps(r.args, sort_keys=True)) for r in case.requests}
    wanted: list[RawRequest] = []
    for r in raw.evidence_requests:
        key = (r.tool, json.dumps(r.args, sort_keys=True))
        if key in done:
            continue
        done.add(key)
        if r.tool not in REQUESTABLE:
            case.requests.append(EvidenceRequest(round=round_, tool=r.tool, args=r.args, reason=r.reason, for_=r.for_, status="refused",
                                                 why=f"{r.tool or 'that'} is not a tool MOKS runs for the review"))
            continue
        if len(wanted) >= max_requests:
            case.requests.append(EvidenceRequest(round=round_, tool=r.tool, args=r.args, reason=r.reason, for_=r.for_, status="not_run",
                                                 why=f"at most {max_requests} requests per review round"))
            continue
        wanted.append(r)
    return wanted


def _challenged(case: Case, item_id: str) -> bool:
    v = case.verdicts.get(item_id)
    return bool(v and v["verdict"] in ("unsupported", "contradicted")) or any(item_id in c["ids"] for c in case.conflicts)


def grade(case: Case, registry: Any, views: dict[str, Any]) -> None:
    """Criteria, confidence and status for every finding and hypothesis, from the evidence and the review."""
    for f in case.findings:
        entries = [registry.records[r] for r in f.citations if r in registry.records]
        live = [e for e in entries if _live(e)]
        f.resource_verified = bool(f.resource) and any(_name(f.resource) in _haystack(r, registry, views) for r in f.citations)
        f.review = (case.verdicts.get(f.id) or {}).get("verdict")
        challenged = _challenged(case, f.id)
        f.criteria = [
            Criterion(name="live_evidence", met=bool(live), weight=FINDING_WEIGHTS["live_evidence"],
                      detail=f"{len(live)} successful live read(s)" if live else "no successful live read is cited"),
            Criterion(name="resource_in_evidence", met=f.resource_verified, weight=FINDING_WEIGHTS["resource_in_evidence"],
                      detail=f"{f.resource or '(no resource named)'} {'occurs' if f.resource_verified else 'does not occur'} in the cited evidence"),
            Criterion(name="complete_result", met=bool(live) and all(e.get("kind") == ev.OBSERVATION and not e.get("truncated") for e in live),
                      weight=FINDING_WEIGHTS["complete_result"], detail="the cited results were complete" if live else "nothing live to be complete"),
            Criterion(name="fresh", met=bool(entries) and all(e.get("kind") != ev.HISTORICAL for e in entries), weight=FINDING_WEIGHTS["fresh"],
                      detail="collected in this attempt" if all(e.get("kind") != ev.HISTORICAL for e in entries) else "collected before the run resumed"),
            Criterion(name="not_challenged", met=not challenged, weight=FINDING_WEIGHTS["not_challenged"],
                      detail="the reviewer marked it " + (f.review or "as conflicting") if challenged else "the reviewer did not challenge it"),
        ]
        f.confidence = _score(f.criteria)
        if challenged:
            f.status = "insufficient_evidence"
        elif live and f.resource_verified:
            f.status = "confirmed_observation"
        elif live or any(_documentation(e) for e in entries):
            f.status = "possible_explanation"
        else:
            f.status = "insufficient_evidence"
    confirmed = {f.id for f in case.findings if f.status == "confirmed_observation"}
    change_refs = {r for c in case.changes if c.before_symptoms for r in c.evidence}
    for h in case.hypotheses:
        v = case.verdicts.get(h.id)
        if v and v["verdict"] in ("supported", "contradicted"):  # what the reviewer cites for its verdict joins the hypothesis's evidence
            side = h.supporting if v["verdict"] == "supported" else h.contradicting
            side += [r for r in v["evidence"] if r in registry.records and r not in h.supporting and r not in h.contradicting]
        entries = [registry.records[r] for r in h.supporting if r in registry.records]
        live = [e for e in entries if _live(e)]
        signals = sorted({signal_of(e["source"]) for e in live})
        against = [r for r in h.contradicting if r in registry.records and registry.records[r].get("ok")]
        h.review = v
        contradicted = bool(against) or (v is not None and v["verdict"] == "contradicted") or any(h.id in c["ids"] for c in case.conflicts)
        upheld = v is not None and v["verdict"] == "supported"
        h.criteria = [
            Criterion(name="live_support", met=bool(live), weight=HYPOTHESIS_WEIGHTS["live_support"],
                      detail=f"supported by {len(live)} live read(s)" if live else "no live evidence supports it"),
            Criterion(name="independent_signals", met=len(signals) >= 2, weight=HYPOTHESIS_WEIGHTS["independent_signals"],
                      detail=f"signals: {', '.join(signals) or 'none'}" + ("" if len(signals) >= 2 else " (needs two kinds)")),
            Criterion(name="confirmed_findings", met=bool(set(h.supporting_findings) & confirmed), weight=HYPOTHESIS_WEIGHTS["confirmed_findings"],
                      detail=", ".join(sorted(set(h.supporting_findings) & confirmed)) or "no confirmed observation is cited"),
            Criterion(name="no_contradiction", met=not contradicted, weight=HYPOTHESIS_WEIGHTS["no_contradiction"],
                      detail=("contradicted by " + ", ".join(against)) if against else "the reviewer found it contradicted" if contradicted else "nothing cited against it"),
            Criterion(name="reviewer_upheld", met=upheld, weight=HYPOTHESIS_WEIGHTS["reviewer_upheld"],
                      detail=f"reviewer: {v['verdict']}" if v else "not reviewed" if not case.reviewed else "the reviewer gave no verdict"),
            Criterion(name="change_precedes", met=bool(set(h.supporting) & change_refs), weight=HYPOTHESIS_WEIGHTS["change_precedes"],
                      detail="a change before the symptoms is cited" if set(h.supporting) & change_refs else "no preceding change is cited"),
        ]
        h.confidence = _score(h.criteria)
        if contradicted or (v is not None and v["verdict"] == "unsupported"):
            h.status = "insufficient_evidence"
        elif live and len(signals) >= 2 and upheld:
            h.status = "strongly_supported"
        elif live or any(_documentation(e) for e in entries):
            h.status = "possible_explanation"
        else:
            h.status = "insufficient_evidence"
    ranked = sorted(case.hypotheses, key=lambda h: (STATUS_ORDER.index(h.status), -h.confidence))
    for i, h in enumerate(ranked, 1):
        h.rank = i
    links: dict[str, list[str]] = {}
    for h in case.hypotheses:
        for fid in h.supporting_findings:
            links.setdefault(fid, []).append(h.id)
        for f in case.findings:
            if f.id not in h.supporting_findings and set(f.citations) & set(h.supporting):
                links.setdefault(f.id, []).append(h.id)
    for f in case.findings:
        f.hypotheses = sorted(set(links.get(f.id, [])), key=lambda x: int(x[1:]))


# ---- what the evidence says by itself ----------------------------------------------------------------

def _views_of(registry: Any, views: dict[str, Any], tools: set[str]) -> list[tuple[str, Any]]:
    return [(ref, views.get(ref)) for ref, e in registry.records.items() if e.get("ok") and e.get("source") in tools]


def onset(registry: Any, views: dict[str, Any], namespace: str, window: tuple[datetime, datetime]) -> datetime | None:
    """The earliest symptom time the evidence shows: a crashed container's end, a warning event about the target, a firing alert."""
    times: list[datetime] = []
    for _, v in _views_of(registry, views, {"k8s_list_pods", "k8s_triage", "k8s_describe_pod"}):
        pods = v if isinstance(v, list) else (v or {}).get("unhealthyPods") if isinstance(v, dict) and "unhealthyPods" in v else [(v or {}).get("summary")] if isinstance(v, dict) else []
        for p in pods or []:
            times += [t for t in (when(x.get("finishedAt")) for x in (p or {}).get("problems") or []) if t]
    for _, v in _views_of(registry, views, {"k8s_events", "k8s_triage"}):
        events = v if isinstance(v, list) else (v or {}).get("warningEvents") if isinstance(v, dict) else []
        times += [t for t in (when(e.get("firstSeen") or e.get("lastSeen")) for e in events or [] if isinstance(e, dict) and e.get("type", "Warning") == "Warning") if t]
    for _, v in _views_of(registry, views, {"prom_alerts"}):
        for rule in v or [] if isinstance(v, list) else []:
            times += [t for t in (when(a.get("activeAt")) for a in rule.get("active") or [] if a.get("state") == "firing" and (a.get("labels") or {}).get("namespace") == namespace) if t]
    earliest = window[0] - timedelta(days=1)
    times = [t for t in times if earliest <= t <= window[1] + timedelta(minutes=5)]
    return min(times) if times else None


def _window_of(minutes: int) -> str:
    return next((w for w in WINDOWS if minutes <= CHANGE_WINDOWS[w]), "older")


def changes(registry: Any, views: dict[str, Any], reference: datetime, lookback: str, symptoms: datetime | None, namespace: str) -> list[Change]:
    """Every change the evidence records within the lookback, each in the smallest window it falls in."""
    limit = CHANGE_WINDOWS.get(lookback, 1440)
    found: list[tuple[datetime, str, str, str, str]] = []
    for ref, v in _views_of(registry, views, {"k8s_rollout_history"}):
        revisions = sorted((v or {}).get("revisions") or [], key=lambda r: r.get("revision") or 0) if isinstance(v, dict) else []
        name = ((v or {}).get("deployment") or {}).get("name") or (registry.records[ref].get("query") or {}).get("deployment") or "?"
        previous: list[str] | None = None
        for r in revisions:
            images = [str(i) for i in r.get("images") or []]
            t = when(r.get("created"))
            if t and previous is not None:
                moved = f"; image {', '.join(previous)} -> {', '.join(images)}" if images != previous else "; same images (configuration or template change)"
                cause = f"; change-cause: {redact(str(r.get('changeCause')))[:120]}" if r.get("changeCause") else ""
                found.append((t, "rollout", f"Deployment/{name}", f"revision {r.get('revision')}{moved}{cause}", ref))
            previous = images
    for ref, v in _views_of(registry, views, {"git_recent_commits"}):
        repo = (registry.records[ref].get("query") or {}).get("repository") or "git"
        for c in v or [] if isinstance(v, list) else []:
            t = when(c.get("date"))
            if t:
                found.append((t, "commit", f"git:{repo}", f"{c.get('short') or str(c.get('sha') or '')[:8]} {redact(str(c.get('message') or ''))[:120]}", ref))
    for ref, v in _views_of(registry, views, {"k8s_argocd_status"}):
        for row in (v or {}).get("problems") or [] if isinstance(v, dict) else []:
            op = row.get("operation") or {}
            t = when(op.get("finished"))
            if t and row.get("namespace") == namespace:
                found.append((t, "gitops_sync", f"Application/{row.get('name')}", f"sync {op.get('phase') or '?'}, {row.get('sync')}/{row.get('health')}", ref))
    out: list[Change] = []
    for t, kind, resource, detail, ref in sorted(found, key=lambda x: x[0], reverse=True):
        minutes = max(0, int((reference - t).total_seconds() // 60))
        if minutes > limit:
            continue
        out.append(Change(id=f"C{len(out) + 1}", kind=kind, resource=resource, at=iso(t), minutes_before=minutes, window=_window_of(minutes), detail=detail,
                          evidence=[ref], before_symptoms=(t <= symptoms) if symptoms else None))
    return out[:30]


def severity(registry: Any, views: dict[str, Any], target: Any, production: bool, proposed: str | None) -> Severity:
    """SEV1-SEV4 from explicit impact criteria; "unknown" when no impact signal could be read at all."""
    criteria: list[str] = []
    outage = degraded = restarting = False
    read_any = False
    for _, v in _views_of(registry, views, {"k8s_workloads", "k8s_triage"}):
        rows = v if isinstance(v, list) else (v or {}).get("workloads") if isinstance(v, dict) else []
        for w in rows or []:
            if isinstance(w, dict) and w.get("name") == target.workload and isinstance(w.get("desired"), int):
                read_any = True
                if w["desired"] > 0 and (w.get("ready") or 0) == 0:
                    outage = True
                    criteria.append(f"{w.get('kind')}/{w['name']}: 0 of {w['desired']} replicas ready")
                elif (w.get("ready") or 0) < w["desired"]:
                    degraded = True
                    criteria.append(f"{w.get('kind')}/{w['name']}: {w.get('ready')} of {w['desired']} replicas ready")
    for _, v in _views_of(registry, views, {"k8s_list_pods"}):
        if isinstance(v, list) and v:
            read_any = True
            if any((p.get("restarts") or 0) > 0 or p.get("problems") for p in v if isinstance(p, dict)):
                restarting = True
                criteria.append("the workload's pods restart or report problems")
    alert_levels: set[str] = set()
    for _, v in _views_of(registry, views, {"prom_alerts"}):
        read_any = True
        for rule in v or [] if isinstance(v, list) else []:
            if any(a.get("state") == "firing" and (a.get("labels") or {}).get("namespace") == target.namespace for a in rule.get("active") or []):
                level = str((rule.get("labels") or {}).get("severity") or "warning").lower()
                alert_levels.add(level)
                criteria.append(f"alert {rule.get('name')} firing ({level})")
    errors, slow, measured = None, False, False
    for _, v in _views_of(registry, views, {"prom_service_signals"}):
        stats = ((v or {}).get("stats") or {}) if isinstance(v, dict) else {}
        stat = stats.get("error_ratio")
        if stat and stat.get("latest") is not None:
            read_any, measured, errors = True, True, float(stat["latest"])
            criteria.append(f"5xx error ratio {errors * 100:.1f}%")
        for key, unit in (("latency_p95_ms", 1.0), ("latency_p95_s", 1000.0)):  # p95 in ms: 1 s or more, or four times the window's best
            p95 = stats.get(key) or {}
            if p95.get("latest") is not None:
                read_any = measured = True
                latest, best = float(p95["latest"]) * unit, float(p95.get("min") or p95["latest"]) * unit
                if latest >= 1000 or (best > 0 and latest >= 4 * best and latest >= 200):
                    slow = True
                    criteria.append(f"p95 latency {latest:.0f} ms" + (f" ({latest / best:.0f}x the window's lowest)" if best > 0 and latest >= 2 * best else ""))
    criteria.append("production environment" if production else "not a production environment")
    critical = "critical" in alert_levels or (errors is not None and errors >= 0.25)
    if not read_any:
        level = "unknown"
        criteria.insert(0, "no workload state, alert or request metric could be read")
    elif production and (outage or critical):
        level = "SEV1"
    elif outage or critical or degraded or alert_levels or (errors is not None and errors >= 0.05):
        level = "SEV2"
    elif restarting or slow or (errors is not None and errors >= 0.01):
        level = "SEV3"
    elif measured or alert_levels:
        level = "SEV4"
        criteria.insert(0, "no impact signal in the evidence")
    else:  # healthy pods alone do not show that users are unaffected
        level = "unknown"
        criteria.insert(0, "the workload looks healthy, but nothing that measures its users' experience could be read")
    proposed = proposed if proposed in ("SEV1", "SEV2", "SEV3", "SEV4") else None
    return Severity(level=level, criteria=list(dict.fromkeys(criteria))[:8], proposed=proposed)


def accept_plan(case: Case, raw: RawPlan, registry: Any, views: dict[str, Any]) -> None:
    case.plan = raw
    case.affected = []
    for a in raw.affected:
        refs = [r for r in a.evidence if r in registry.records and registry.records[r].get("ok")]
        if a.resource and refs and any(_name(a.resource) in _haystack(r, registry, views) for r in refs):
            case.affected.append({"resource": a.resource, "reason": a.reason, "evidence": refs})
        elif a.resource:
            case.unsupported.append({"statement": f"affected: {a.resource}"[:200], "task": "plan", "reason": "not named in the evidence it cites"})


def timeline(case: Case, registry: Any, views: dict[str, Any]) -> list[TimelineEvent]:
    events: list[TimelineEvent] = [TimelineEvent(at=c.at, event=f"{c.kind.replace('_', ' ')}: {c.resource} ({c.detail})", evidence=c.evidence, source="evidence")
                                   for c in case.changes]
    if case.onset:
        events.append(TimelineEvent(at=iso(case.onset), event="first symptom in the evidence", evidence=[], source="evidence"))
    for ref, v in _views_of(registry, views, {"k8s_list_pods"}):
        for p in v or [] if isinstance(v, list) else []:
            for x in (p or {}).get("problems") or []:
                t = when(x.get("finishedAt"))
                if t:
                    events.append(TimelineEvent(at=iso(t), event=f"pod {p.get('name')}: container {x.get('container')} {x.get('state')} ({x.get('reason')}, exit {x.get('exitCode')})",
                                                evidence=[ref], source="evidence"))
    for item in (case.rca.timeline if case.rca else []):
        t = when(item.at)
        refs = [r for r in item.evidence if r in registry.records]
        if t and refs and item.event:
            events.append(TimelineEvent(at=iso(t), event=item.event, evidence=refs, source="rca"))
    seen: set[tuple[str, str]] = set()
    out = []
    for e in sorted(events, key=lambda e: e.at):
        if (e.at, e.event) not in seen:
            seen.add((e.at, e.event))
            out.append(e)
    return out[-40:]


# ---- the report ------------------------------------------------------------------------------------

CONFIDENCE = {"strongly_supported": "high", "possible_explanation": "medium", "insufficient_evidence": "low"}


def root_cause(case: Case) -> dict[str, Any] | None:
    by_id = {h.id: h for h in case.hypotheses}
    chosen = by_id.get(case.rca.root_cause) if case.rca and case.rca.root_cause else None
    if chosen is not None and chosen.status == "insufficient_evidence":
        case.unsupported.append({"statement": f"root cause {chosen.id}: {chosen.statement}"[:200], "task": "rca", "reason": "the hypothesis it names has insufficient evidence"})
        chosen = None
    if chosen is None:
        chosen = next((h for h in sorted(case.hypotheses, key=lambda h: h.rank) if h.status == "strongly_supported"), None)
    if chosen is None:
        return None
    return {"hypothesis": chosen.id, "statement": chosen.statement, "status": chosen.status, "label": STATUS_LABEL[chosen.status], "confidence": chosen.confidence}


def _summary(case: Case, cause: dict[str, Any] | None) -> str:
    sev = f"{case.severity.level}. " if case.severity else ""
    if cause:
        head = f"{STATUS_LABEL[cause['status']].capitalize()} ({cause['hypothesis']}, not confirmed): {cause['statement']}"
    else:
        top = next((h for h in sorted(case.hypotheses, key=lambda h: h.rank) if h.status == "possible_explanation"), None)
        head = f"No root cause is established. Possible explanation ({top.id}): {top.statement}" if top else "The evidence collected does not support a root cause yet."
    backing = set(next((h.supporting for h in case.hypotheses if cause and h.id == cause["hypothesis"]), []))
    confirmed = [f for f in case.findings if f.status == "confirmed_observation"]
    facts = [f.observation.rstrip(".") for f in sorted(confirmed, key=lambda f: not set(f.citations) & backing)][:2]
    return (sev + head.rstrip(".") + "." + (" Observed: " + "; ".join(facts) + "." if facts else ""))[:2000]


def to_report(case: Case, registry: Any, notes: list[str]) -> tuple[Report, dict[str, Any], dict[str, Any] | None]:
    """The Report MOKS stores: confirmed observations as facts, graded hypotheses as causes, the RCA's words only where
    the grading allows them. Returns (report, what validation decided, the root cause)."""
    cause = root_cause(case)
    summary = case.rca.summary if case.rca and case.rca.summary else ""
    if summary and ev.CONFIRMED.search(summary):
        case.unsupported.append({"statement": summary[:200], "task": "rca", "reason": "the summary calls the root cause confirmed or resolved; MOKS never confirms a hypothesis"})
        summary = ""
    if summary and cause is None and case.rca and case.rca.root_cause:
        summary = ""  # the RCA's story rests on a root cause the evidence did not support
    summary = summary or _summary(case, cause)
    facts = [Fact(statement=f.observation, evidence=list(f.citations)) for f in case.findings if f.status == "confirmed_observation"][:15]
    causes = []
    for h in sorted(case.hypotheses, key=lambda h: h.rank):
        if h.status == "insufficient_evidence":
            continue
        live = any(_live(registry.records[r]) for r in h.supporting if r in registry.records)
        confidence = CONFIDENCE[h.status] if live or h.status != "possible_explanation" else "low"
        met = [c.name.replace("_", " ") for c in h.criteria if c.met]
        why = f"{STATUS_LABEL[h.status]} (confidence {h.confidence:.2f}; met: {', '.join(met) or 'none'})"
        if h.review and h.review.get("reason"):
            why += f". Reviewer: {h.review['reason']}"
        causes.append(Cause(cause=h.statement, confidence=confidence, evidence=list(h.supporting), reasoning=why[:1200], contradicting=list(h.contradicting), origin="model"))
    missing = list(dict.fromkeys([*case.missing, *case.gaps, *(f"{g['source']}: {g['reason']}" for g in registry.gaps)]))[:25]
    steps = list(case.rca.next_steps) if case.rca else []
    steps += [f"To settle {h.id}: {h.would_confirm}" for h in sorted(case.hypotheses, key=lambda h: h.rank) if h.status != "strongly_supported" and h.would_confirm][:3]
    removed = [u for u in case.unsupported]
    if removed or case.rejected:
        notes.append(f"Incident review checked by MOKS: {len(case.rejected)} invented citation(s) rejected, {len(removed)} unsupported claim(s) set aside.")
    report = Report(summary=summary, observed_facts=facts, suspected_causes=causes, missing_evidence=missing, next_steps=list(dict.fromkeys(steps))[:8],
                    generated_by="llm", notes=list(notes))
    report.validation = {"valid_refs": sorted(registry.records, key=lambda r: int(r[1:])), "method": "crewai incident: criteria in code (docs/crewai-sre-engine.md)"}
    decided = {"citations_rejected": list(case.rejected), "claims_removed": removed,
               "confidence_lowered": [{"statement": h.statement[:200], "reason": f"graded {STATUS_LABEL[h.status]} by MOKS"} for h in case.hypotheses if h.status != "strongly_supported"]}
    return report, decided, cause


def usable(case: Case) -> bool:
    return bool(case.findings or case.hypotheses)

