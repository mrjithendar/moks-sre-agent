"""The shared SRE knowledge base (moks-rag) as a stage of triage and root-cause analysis (docs/SHARED_KNOWLEDGE.md).

Which investigations: those an alert or a detection started, those attached to an incident, CrewAI console flows and
any troubleshooting request. Guidance and concept answers keep to MOKS's own index.

When:
  initial  before live evidence is read, from what the alert or the incident says (LangGraph's knowledge node)
  refresh  after triage and logs, when the codes and error lines MOKS found add to what was asked (LangGraph's refresh)
  final    after the engine, for the CrewAI and unified engines (no knowledge node of their own), with every error line
           the run collected
At most two searches per investigation. Like the knowledge stage's, they have their own allowance and never take a call
from the live-evidence budget.

The query is built by MOKS, never by a model: the alert's name, namespace, workload and pod, then the exact codes and
the key error lines (moks/rag/shared.py). Each search is a shared_kb_search call through the executor, so it is
authorized, audited, redacted, scoped and stored as evidence the answer can cite like any other: as documentation,
which can never back an observed fact.

What comes back reaches the answer three ways:
  - LangGraph's planner and report writer see the search in their evidence, before they write;
  - the report's next steps get the best runbooks' steps and the past incidents, each labelled as documented or
    historical, never observed, with the evidence id of the search;
  - payload["shared_knowledge"] lists the runbooks, past incidents and manifests, with what each matched.

When moks-rag is unreachable the search fails like any tool call: the gap is recorded, the report says the triage rests
on the alert, the logs and the cluster alone, and the investigation goes on.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..observability import KNOWLEDGE_RETRIEVALS
from ..rag import shared
from ..redaction import redact

log = logging.getLogger("moks.orchestrator.shared_knowledge")

TOOL = "shared_kb_search"
STAGES = ("initial", "refresh", "final")
MAX_SEARCHES = 2
MIN_SECONDS_LEFT = shared.TIMEOUT_SECONDS + 5  # a search started closer to the time limit would take the report's time
TRIAGE_TRIGGERS = {"alert", "detection"}
LOG_TOOLS = {"k8s_pod_logs", "loki_search", "loki_query", "cw_logs_query"}
STATE_TOOLS = {"k8s_describe_pod", "k8s_events", "k8s_triage", "k8s_list_pods", "k8s_workloads"}
FLAT_CHARS = 20_000           # read from one evidence item when looking for error lines
ARG_LINES = 12                # lines handed to the tool; it keeps the most telling MAX_ERROR_LINES for the query text
RUNBOOK_STEPS = 2             # runbooks whose procedure goes into the next steps
INCIDENT_LINKS = 3
K8S_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?")
ALERT_NAME = re.compile(r"[A-Za-z_:][A-Za-z0-9_:.-]{0,199}")
WORKLOAD_LABELS = ("deployment", "statefulset", "daemonset", "job_name", "cronjob", "workload", "app_kubernetes_io_name")
SERVICE_LABELS = ("service", "app", "app_kubernetes_io_name", "k8s_app")
REASONS = {
    "off": "Not searched: {reason}.",
    "not_triage": "Not searched: this request is not a triage or root-cause analysis.",
    "time": "Not searched: the investigation was too close to its time limit.",
    "nothing": "Not searched: the alert and the evidence named nothing to search for.",
    "nothing_new": "Not searched again: the evidence added no code or error line the first search had not used.",
    "budget": "Not searched: the same search already ran, or the tool-call budget was used up.",
}


def new_state() -> dict[str, Any]:
    return {"status": "not_attempted", "reason": None, "searches": [], "calls": 0}


def applies(inv: Any) -> str | None:
    """None when this investigation searches moks-rag; otherwise why it does not."""
    if not shared.enabled():
        return REASONS["off"].format(reason=shared.why_off())
    req = getattr(inv, "request", None)
    if req is not None and req.workflow in ("guidance", "concept"):
        return "Not searched: guidance and concept answers use MOKS's own knowledge index."
    triage = getattr(inv, "trigger", None) in TRIAGE_TRIGGERS or bool(getattr(inv, "incident_id", None)) or bool(getattr(inv, "crew_request", None))
    if not triage and not (req is not None and getattr(req, "response_type", None) == "troubleshooting"):
        return REASONS["not_triage"]
    return None


# ---- what to search for -----------------------------------------------------------------------------------


def _name(value: Any, rx: re.Pattern[str] = K8S_NAME) -> str | None:
    v = str(value or "").strip()
    return v if v and rx.fullmatch(v) else None


def _flatten(data: Any, out: list[str], budget: list[int], key: str = "", focus: str | None = None) -> None:
    """Strings as lines and scalars as "key: value", so a describe's exitCode 137 reads as a line BM25 can match. With a
    focus (the alert's workload), a namespace-wide list keeps only the rows that name it: another pod's codes stay out."""
    if budget[0] <= 0:
        return
    if isinstance(data, dict):
        if isinstance(data.get("errorLines"), list):  # k8s_pod_logs: the lines that already look like errors
            data = {"errorLines": data["errorLines"]}
        for k, v in data.items():
            _flatten(v, out, budget, str(k), focus)
    elif isinstance(data, list):
        for v in data[:200]:
            if focus and isinstance(v, dict) and focus not in json.dumps(v, default=str):
                continue
            _flatten(v, out, budget, key, focus)
    elif isinstance(data, str):
        text = data[:budget[0]]
        budget[0] -= len(text)
        out.append(f"{key}: {text}" if key in ("reason", "message", "exitCode", "signal") else text)
    elif isinstance(data, (int, float)) and not isinstance(data, bool) and key in ("exitCode", "signal"):
        out.append(f"{key}: {data}")


def evidence_lines(inv: Any, focus: str | None = None) -> list[str]:
    """Error-looking lines from what this run collected about its target: findings first, then logs, then cluster state.
    With a focus (the workload), only findings, pods and log reads that name it."""
    req = getattr(inv, "request", None)

    def mine(e: dict) -> bool:
        args = e.get("args") or {}
        ns = args.get("namespace")
        if not (req is None or ns is None or req.in_scope(ns)):
            return False
        pod = args.get("pod") or args.get("object_name")
        return not (focus and isinstance(pod, str) and focus not in pod)

    found = [f"{f.get('title') or ''}. {f.get('detail') or ''}" for f in inv.findings
             if (req is None or f.get("namespace") is None or req.in_scope(f.get("namespace"))) and (not focus or focus in json.dumps(f, default=str))]
    logs: list[str] = []
    state: list[str] = []
    for e in inv.evidence:
        if not e.get("ok") or e.get("tool") not in LOG_TOOLS | STATE_TOOLS or not mine(e):
            continue
        out: list[str] = []
        _flatten(e.get("data"), out, [FLAT_CHARS], focus=focus)
        (logs if e["tool"] in LOG_TOOLS else state).extend(out)
    return [*found, *reversed(logs), *state]  # the newest log lines first


def triage_args(inv: Any, stage: str) -> dict[str, Any]:
    """shared_kb_search arguments from what MOKS resolved: the alert's labels, the incident, the request and, after the
    initial stage, the error lines the run collected. Values that do not fit the tool's contract are left out."""
    alert = getattr(inv, "alert_info", None) or {}
    labels, ann = alert.get("labels") or {}, alert.get("annotations") or {}
    inc = getattr(inv, "incident_info", None) or {}
    req = getattr(inv, "request", None)
    focus = inv.focus_namespace if inv.focus_namespace not in (None, "*") else None
    namespace = _name(labels.get("namespace") or labels.get("kubernetes_namespace")) or _name(focus) or _name(inc.get("namespace") if inc.get("namespace") != "*" else None)
    resource = str(inc.get("resource") or "")
    workload = (next((_name(labels.get(k)) for k in WORKLOAD_LABELS if _name(labels.get(k))), None)
                or _name(resource.partition("/")[2] if "/" in resource else None)
                or _name(getattr(inv, "crew_workload", None))
                or _name(getattr(req, "target_name", None) if req is not None else None))
    pod = _name(labels.get("pod"))
    service = next((_name(labels.get(k)) for k in SERVICE_LABELS if _name(labels.get(k))), None)
    said = [str(ann.get(k) or "") for k in ("description", "message")] + [str(inc.get("summary") or "")]
    focus = workload or shared.workload_of(pod)
    lines = shared.error_lines([*said, *(evidence_lines(inv, focus) if stage != "initial" else [])], ARG_LINES)
    if not lines:  # the description still says what is wrong in words, when no line looks like an error
        lines = [redact(" ".join(s.split()))[:400] for s in said if s.strip()][:2]
    question = inv.request.question if req is not None else ""
    text = str(ann.get("summary") or inc.get("title") or question or "")
    args = {"namespace": namespace, "alertname": _name(labels.get("alertname"), ALERT_NAME), "workload": workload, "pod": pod,
            "container": _name(labels.get("container")), "service": service if service != workload else None,
            "errors": [x[:400] for x in lines][:ARG_LINES], "text": redact(" ".join(text.split()))[:600]}
    return {k: v for k, v in args.items() if v not in (None, "", [])}


def _signature(args: dict[str, Any]) -> set[str]:
    q = shared.build_query(shared.Triage(**{k: v for k, v in args.items() if k != "k"}))
    return {i.lower() for i in q.identifiers} | {re.sub(r"\d+", "#", e.lower()) for e in q.errors}


# ---- the stage ----------------------------------------------------------------------------------------------


def _prior(inv: Any, args: dict[str, Any]) -> dict | None:
    """The same search stored before a crash or a retry: a resumed run reads it rather than asking again."""
    from ..tools.phase2 import SharedKbArgs

    want = SharedKbArgs.model_validate(args).model_dump(exclude_none=True)
    return next((e for e in inv.evidence if e.get("tool") == TOOL and e.get("args") == want), None)


def _skip(st: dict, stage: str, reason: str) -> None:
    st["searches"].append({"stage": stage, "status": "skipped", "reason": reason})
    KNOWLEDGE_RETRIEVALS.labels(f"shared_{stage}", "skipped").inc()


async def run(inv: Any, executor: Any, stage: str) -> None:
    """One bounded search. Never raises for a retrieval problem: the gap is recorded and the investigation goes on."""
    st = inv.shared_kb
    done = {s["stage"] for s in st["searches"]}
    if stage in done or (stage == "final" and any(s.get("ref") for s in st["searches"])):
        return
    if sum(1 for s in st["searches"] if s.get("ref")) >= MAX_SEARCHES:
        return
    why = applies(inv)
    if why:
        st["reason"] = st["reason"] or why
        return
    if inv.time_left() < MIN_SECONDS_LEFT:
        return _skip(st, stage, REASONS["time"])
    args = triage_args(inv, stage)
    if not any(args.get(k) for k in ("alertname", "workload", "pod", "errors", "text")):
        return _skip(st, stage, REASONS["nothing"])
    earlier = [s for s in st["searches"] if s.get("args")]
    if earlier and not (_signature(args) - set().union(*(_signature(s["args"]) for s in earlier))):
        return _skip(st, stage, REASONS["nothing_new"])
    prior = _prior(inv, args)
    if prior is None:
        st["calls"] += 1  # reserved first: the call comes from this stage's allowance, not the live budget
        label = "Searching the shared SRE knowledge base (moks-rag)" + (" for the errors triage found" if stage != "initial" else "")
        await inv._emit("phase", {"phase": "knowledge", "label": label})
        out = await inv._call(executor, TOOL, args, f"shared SRE knowledge ({stage}): runbooks, postmortems and manifests for this alert's names, codes and error lines")
        if out is None:
            st["calls"] -= 1
            return _skip(st, stage, REASONS["budget"])
        rec = {"ref": out.evidence_ref, "ok": out.ok, "data": out.data, "source": out.source or {}, "error": out.error, "error_kind": out.error_kind}
    else:
        rec = {**prior, "reused": True}
    rows = rec["data"] if rec["ok"] and isinstance(rec.get("data"), list) else []
    src = rec.get("source") or {}
    status = "failed" if not rec["ok"] else "ok" if rows else "empty"
    st["searches"].append({"stage": stage, "ref": rec.get("ref"), "status": status, "args": args, "query": src.get("query"), "identifiers": src.get("identifiers") or [],
                           "results": len(rows), "filtered_out": src.get("filtered_out", 0), "latency_ms": src.get("latency_ms"), "reused": bool(rec.get("reused")),
                           "error_kind": rec.get("error_kind"), "reason": None if rec["ok"] else str(rec.get("error") or "")[:300]})
    st["status"] = _overall(st)
    KNOWLEDGE_RETRIEVALS.labels(f"shared_{stage}", status).inc()
    if status == "failed":
        note = f"The shared SRE knowledge base (moks-rag) could not be searched ({rec.get('error_kind') or 'error'}); triage goes on with the alert, logs and cluster evidence."
        if note not in inv.notes:
            inv.notes.append(note)
        log.warning("shared knowledge search failed", extra={"event": "shared_knowledge_failed", "investigation_id": str(inv.id), "stage": stage,
                                                              "errorCode": rec.get("error_kind") or "error"})


def _overall(st: dict) -> str:
    ran = [s for s in st["searches"] if s.get("ref")]
    if any(s["status"] == "ok" for s in ran):
        return "ok"
    if any(s["status"] == "empty" for s in ran):
        return "empty"
    if ran:
        return "failed"
    return "skipped" if st["searches"] else st["status"]


# ---- what the answer gets ------------------------------------------------------------------------------------


def passages(inv: Any) -> list[dict[str, Any]]:
    """Every passage the searches returned, the later (better-informed) search first, each document chunk once."""
    st = inv.shared_kb
    stored = {e["ref"]: e for e in inv.evidence if e.get("tool") == TOOL and e.get("ok") and e.get("ref")}
    seen: set[Any] = set()
    out = []
    for s in reversed([s for s in st["searches"] if s.get("ref") in stored]):
        for i, p in enumerate(stored[s["ref"]].get("data") or []):
            if not isinstance(p, dict):
                continue
            key = p.get("id") or (p.get("doc_id"), i)
            if key in seen:
                continue
            seen.add(key)
            out.append({**p, "ref": s["ref"]})
    return out


def _md(text: Any) -> str:
    """Text that cannot open or close a Markdown link."""
    return re.sub(r"[\[\]()<>]", "", " ".join(str(text or "").split()))[:200]


def runbook_step(p: dict[str, Any]) -> str:
    matched = p.get("matched") or []
    why = f"matches {', '.join(matched[:4])}" if matched else "ranked by meaning; it names none of the exact codes"
    steps = "; ".join(f"({i}) {s}" for i, s in enumerate(p.get("steps") or [], 1))
    link = f" [Open]({p['url']})." if p.get("url") else ""
    return (f"Runbook **{_md(p.get('title') or p.get('doc_id'))}** from the shared SRE knowledge base ({p['ref']}; {why}): {steps}.{link} "
            "Documented, not observed: check each step against the live evidence before following it.")


def incidents_step(rows: list[dict[str, Any]]) -> str:
    parts = []
    for p in rows:
        m = p.get("metadata") or {}
        what = _md(p.get("title") or m.get("incident_id") or p.get("doc_id"))
        facts = ", ".join(str(m[k]) for k in ("date", "severity", "service") if m.get(k))
        parts.append((f"[{what}]({p['url']})" if p.get("url") else what) + (f" ({_md(facts)})" if facts else ""))
    refs = ", ".join(dict.fromkeys(p["ref"] for p in rows))
    return (f"Past incidents in the shared SRE knowledge base ({refs}): {'; '.join(parts)}. "
            "History, not the current cause: compare their symptoms with this one before reusing their fix.")


def _brief(p: dict[str, Any]) -> dict[str, Any]:
    return {k: p.get(k) for k in ("ref", "doc_id", "kind", "title", "url", "matched", "steps", "rank", "score", "metadata")} | {"excerpt": str(p.get("text") or "")[:300]}


def outcome(inv: Any, report: Any, payload: dict[str, Any]) -> None:
    """The runbook steps and past incidents into the next steps, a gap when moks-rag could not be read, and the summary."""
    st = inv.shared_kb
    found = passages(inv)
    runbooks = [p for p in found if p.get("kind") in ("runbook", "document", "architecture")]
    incidents = [p for p in found if p.get("kind") == "postmortem"]
    manifests = [p for p in found if p.get("kind") == "manifest"]
    added: list[str] = []
    if payload.get("response_type") in (None, "troubleshooting") or applies(inv) is None:
        added += [runbook_step(p) for p in [r for r in runbooks if r.get("steps")][:RUNBOOK_STEPS]]
        if incidents:
            added.append(incidents_step(incidents[:INCIDENT_LINKS]))
    if added:
        report.next_steps = [*report.next_steps, *added]
        payload["next_steps"] = report.next_steps
    if st["status"] == "failed":
        gap = "The shared SRE knowledge base (moks-rag) could not be searched, so no shared runbook or past postmortem was checked; this triage rests on the alert, logs and cluster evidence only."
        payload["missing_evidence"] = [*(payload.get("missing_evidence") or []), gap]
    payload["shared_knowledge"] = {
        "status": st["status"], "reason": st.get("reason"), "note": shared.NOTE, "collection": shared.status()["collection"],
        "searches": [{k: v for k, v in s.items() if k != "args"} for s in st["searches"]],
        "runbooks": [_brief(p) for p in runbooks[:8]], "incidents": [_brief(p) for p in incidents[:8]], "manifests": [_brief(p) for p in manifests[:8]],
        "counts": {"passages": len(found), "runbooks": len(runbooks), "incidents": len(incidents), "manifests": len(manifests),
                   "exact": sum(1 for p in found if p.get("matched")), "next_steps_added": len(added)},
    }
    inv.trace["shared_knowledge"] = {"status": st["status"], "searches": len([s for s in st["searches"] if s.get("ref")]), "passages": len(found), "next_steps_added": len(added)}
