"""The incident crew ("sre"): seven agents and the ten-step workflow of docs/crewai-sre-engine.md.

What an admin may change in the CrewAI console is the same as for the workload crew (crew/config.py): roles, goals,
backstories, task texts, each agent's tools within its allowlist, and the limits within their ceilings. What stays in
code: the agents and the order of the tasks, the evidence-review loop and its bounds, every task's JSON output
contract, the untrusted-data rule, and everything decided about the answer (crew/validation.py).
"""
from __future__ import annotations

from typing import Any

from ..config import get_settings

CREW = "sre"
FLOW = "incident_investigation"

AGENT_KEYS = ("commander", "kubernetes", "observability", "change", "knowledge", "reviewer", "rca")
AGENT_LABELS = {"commander": "Incident commander", "kubernetes": "Kubernetes investigator", "observability": "Observability investigator",
                "change": "Change analyst", "knowledge": "Historical knowledge analyst", "reviewer": "Evidence reviewer", "rca": "Root cause analyst"}
# Workflow order (section 3 of the doc). The commander plans first and forms the hypotheses once the specialists are done;
# the review may run again after MOKS has collected the evidence it asked for.
TASK_KEYS = ("plan", "kubernetes", "observability", "change", "knowledge", "hypotheses", "review", "rca")
TASK_AGENT = {"plan": "commander", "kubernetes": "kubernetes", "observability": "observability", "change": "change", "knowledge": "knowledge",
              "hypotheses": "commander", "review": "reviewer", "rca": "rca"}
TASK_LABELS = {"plan": "Plan the investigation", "kubernetes": "Kubernetes evidence", "observability": "Metrics, logs and traces", "change": "Recent changes",
               "knowledge": "Historical incidents and runbooks", "hypotheses": "Root-cause hypotheses", "review": "Independent evidence review", "rca": "Root-cause analysis"}
SPECIALISTS = ("kubernetes", "observability", "change", "knowledge")
STRUCTURED = ("plan", "hypotheses", "review", "rca")  # tasks whose answer is a decision, sized like a report

TOOLS_BY_AGENT: dict[str, list[str]] = {
    "commander": ["namespace_health", "workload_status", "firing_alerts"],
    "kubernetes": ["workload_pods", "describe_pod", "recent_events", "pod_logs", "pod_metrics", "node_health", "autoscaling", "storage", "services",
                   "namespace_health", "workload_status"],
    "observability": ["service_signals", "search_logs", "trace_logs", "pod_metrics", "metric", "firing_alerts", "grafana_alerts", "dashboards",
                      "datastores", "postgres_health", "redis_health", "ml_signals"],
    "change": ["rollout_history", "changed_commits", "deployed_commit", "config_audit", "gitops_status", "recent_events"],
    "knowledge": ["search_runbooks", "shared_runbooks", "similar_incidents", "incident_history"],
    "reviewer": [],  # evidence_list and evidence_detail read the registry only and are always there
    "rca": [],
}
# What the reviewer may ask MOKS to collect: any specialist's tool, run by MOKS with the same binding and checks.
REQUESTABLE = sorted({t for k in SPECIALISTS for t in TOOLS_BY_AGENT[k]})

CHANGE_WINDOWS = {"15m": 15, "1h": 60, "6h": 360, "24h": 1440}

FINDINGS_CONTRACT = (
    "Finish with one JSON object and nothing else:\n"
    '{"findings": [{"observation": "one thing the evidence shows", "resource": "Kind/name it is about", "evidence": ["E3"], '
    '"at": "ISO-8601 time the evidence shows, or null"}], "gaps": ["what you could not see, and why"]}\n'
    "Every finding cites at least one evidence id MOKS gave you. An observation is what a tool returned, not what you infer: "
    "inferences belong to the hypotheses. Do not quote evidence: MOKS attaches the excerpts itself.")
CONTRACTS = {
    "plan": ("Finish with one JSON object and nothing else:\n"
             '{"severity": "SEV1|SEV2|SEV3|SEV4", "affected": [{"resource": "Kind/name", "reason": "why it is affected", "evidence": ["E1"]}], '
             '"symptom_start": "ISO-8601 or null", "objectives": ["one question a specialist must answer"]}\n'
             "MOKS also grades severity from the evidence; yours is recorded beside it."),
    "kubernetes": FINDINGS_CONTRACT, "observability": FINDINGS_CONTRACT, "change": FINDINGS_CONTRACT, "knowledge": FINDINGS_CONTRACT,
    "hypotheses": ("Finish with one JSON object and nothing else:\n"
                   '{"hypotheses": [{"statement": "a possible root cause", "supporting": ["F1", "E3"], "contradicting": ["F4"], '
                   '"would_confirm": "the evidence that would settle it"}]}\n'
                   "Cite finding ids (F1, F2, …) or evidence ids. At most five hypotheses, most likely first. A hypothesis is never a fact."),
    "review": ("Finish with one JSON object and nothing else:\n"
               '{"verdicts": [{"id": "F1 or H1", "verdict": "supported|unsupported|contradicted|insufficient", "reason": "...", "evidence": ["E2"]}], '
               '"conflicts": [{"ids": ["F1", "F4"], "description": "how they disagree"}], "missing_telemetry": ["a signal no tool could read"], '
               '"evidence_requests": [{"tool": "pod_logs", "args": {"pod": "..."}, "reason": "what it would settle", "for": "H1"}]}\n'
               "Give a verdict for every hypothesis. Ask for evidence only when it would change a verdict; MOKS runs at most a few requests "
               "and only with tools it allows: " + ", ".join(REQUESTABLE) + "."),
    "rca": ("Finish with one JSON object and nothing else:\n"
            '{"summary": "what happened, for an incident channel", "root_cause": "H1 or null", "contributing": ["H2"], '
            '"timeline": [{"at": "ISO-8601", "event": "...", "evidence": ["E2"]}], "impact": "who or what was affected", "next_steps": ["the next diagnostic step"]}\n'
            "Choose the root cause only from the hypotheses, by id; choose null when none is supported. MOKS decides how strongly it may be stated."),
}


def duration_ceiling() -> int:
    s = get_settings()
    return min(1800, s.crewai_max_duration_seconds or s.max_duration_seconds)


def ceilings() -> dict[str, tuple[int, int]]:
    s = get_settings()
    return {"timeout_seconds": (30, duration_ceiling()), "task_timeout_seconds": (10, 600), "max_tool_calls": (1, s.max_tool_calls),
            "max_tool_calls_per_agent": (1, s.max_tool_calls), "max_iterations": (1, 20), "step_tokens": (100, 4000), "report_tokens": (200, 6000),
            "token_budget": (5000, 2_000_000), "review_rounds": (1, 3), "followup_calls": (0, 8), "model_retries": (0, 3), "output_retries": (0, 3)}


def defaults() -> dict[str, Any]:
    s = get_settings()
    rules_of_evidence = ("You cite evidence only by the ids MOKS gives you. You never present an assumption as a fact, and you say plainly "
                         "what you could not see.")
    return {
        "agents": {
            "commander": {"role": AGENT_LABELS["commander"], "tools": list(TOOLS_BY_AGENT["commander"]),
                          "goal": "Understand the incident around {target}, scope it, and turn the specialists' findings into testable hypotheses.",
                          "backstory": "You run incidents. You classify severity from impact, name what is affected, and give each specialist a clear "
                                       "question. " + rules_of_evidence},
            "kubernetes": {"role": AGENT_LABELS["kubernetes"], "tools": list(TOOLS_BY_AGENT["kubernetes"]),
                           "goal": "Establish the Kubernetes state of {target}: pods, restarts, crash reasons, scheduling, nodes, autoscaling and storage.",
                           "backstory": "You know CrashLoopBackOff, OOMKilled, ImagePullBackOff, FailedScheduling, node pressure, HPA and KEDA limits and "
                                        "unbound volumes by their signatures. " + rules_of_evidence},
            "observability": {"role": AGENT_LABELS["observability"], "tools": list(TOOLS_BY_AGENT["observability"]),
                              "goal": "Measure {target}: error rate, latency percentiles, throughput, saturation, its logs and its dependencies.",
                              "backstory": "You read golden signals and logs, follow a request by its trace id, and check the databases, caches and queues a "
                                           "service depends on. Missing telemetry is a finding, not a reason to guess. " + rules_of_evidence},
            "change": {"role": AGENT_LABELS["change"], "tools": list(TOOLS_BY_AGENT["change"]),
                       "goal": "Find every change around {target} in the change window: rollouts, images, configuration, commits, GitOps syncs, infrastructure.",
                       "backstory": "You know most incidents follow a change. You record what changed and when; MOKS places each change in its window. "
                                    + rules_of_evidence},
            "knowledge": {"role": AGENT_LABELS["knowledge"], "tools": list(TOOLS_BY_AGENT["knowledge"]),
                          "goal": "Find past incidents, root-cause analyses and runbooks that match the symptoms of {target}.",
                          "backstory": "You know documentation and past incidents are leads, not proof of what is happening now. " + rules_of_evidence},
            "reviewer": {"role": AGENT_LABELS["reviewer"],
                         "goal": "Challenge every finding and hypothesis about {target} against the evidence, independently of the agents who wrote them.",
                         "backstory": "You are the sceptic. You separate facts from hypotheses, find unsupported assumptions and conflicting observations, "
                                      "check that citations say what they are cited for, and name the telemetry that is missing. " + rules_of_evidence},
            "rca": {"role": AGENT_LABELS["rca"],
                    "goal": "Write the root-cause analysis of the incident around {target} that the evidence supports, and no more.",
                    "backstory": "You write for the people handling the incident: what happened, when, why as far as the evidence shows, and what to do next. "
                                 + rules_of_evidence},
        },
        "tasks": {
            "plan": {"description": "Incident: {target}.\n{question}\nCheck the namespace health, the workload status and firing alerts. Then classify severity, "
                                    "list the affected resources and write one objective per specialist (Kubernetes, observability, changes, history).",
                     "expected_output": "The investigation plan as JSON."},
            "kubernetes": {"description": "Incident: {target}.\nCollect the Kubernetes evidence: pod states and restarts, crash reasons (describe_pod; pod_logs with "
                                          "previous=true for a crashing pod), events, scheduling, node pressure, autoscaling and storage.",
                           "expected_output": "Findings as JSON, each citing its evidence."},
            "observability": {"description": "Incident: {target}.\nMeasure error rate, latency and throughput (service_signals), saturation (pod_metrics), search "
                                             "the logs for errors, and check the dependencies the logs point at (datastores, queues via metric). ml_signals "
                                             "shows statistical anomalies and the correlated group the workload is in: leads to confirm with the live tools.",
                              "expected_output": "Findings as JSON, each citing its evidence."},
            "change": {"description": "Incident: {target}.\nFind rollouts and image changes, commits (also under deploy/, helm/ or terraform/), configuration "
                                      "problems and Argo CD syncs in the change window.",
                       "expected_output": "Findings as JSON: each change with its time and evidence."},
            "knowledge": {"description": "Incident: {target}.\nSearch for similar past incidents and for runbooks that match the symptoms in the case file. "
                                        "incident_history shows recurring failures of this workload and resolutions that worked before. shared_runbooks searches "
                                        "the shared SRE knowledge base: pass it the exact error lines and codes the case file shows.",
                          "expected_output": "Findings as JSON: each lead with its evidence."},
            "hypotheses": {"description": "Incident: {target}.\nFrom the findings in the case file, write the root-cause hypotheses. For each, cite what supports "
                                          "and what contradicts it, and what evidence would settle it.",
                           "expected_output": "The hypotheses as JSON."},
            "review": {"description": "Incident: {target}.\nReview every finding and hypothesis in the case file against the evidence (evidence_list, "
                                      "evidence_detail). Give a verdict for each hypothesis, list conflicts and missing telemetry, and ask for evidence only "
                                      "when it would change a verdict.",
                       "expected_output": "The review as JSON."},
            "rca": {"description": "Incident: {target}.\n{question}\nWrite the root-cause analysis from the case file: the hypotheses with MOKS's grading, "
                                   "the review and the timeline.",
                    "expected_output": "The root-cause analysis as JSON."},
        },
        "limits": {"timeout_seconds": min(600, duration_ceiling()), "task_timeout_seconds": 120, "max_tool_calls": min(32, s.max_tool_calls),
                   "max_tool_calls_per_agent": min(8, s.max_tool_calls), "max_iterations": 6, "step_tokens": 900, "report_tokens": 1800,
                   "token_budget": 150_000, "review_rounds": 2, "followup_calls": 4, "model_retries": 1, "output_retries": 1},
        "defaults": {"kind": "Deployment", "minutes": None, "change_window": "24h"},
    }


FLOW_SPEC: dict[str, Any] = {
    "label": "Incident investigation",
    "crew": CREW,
    "description": "Seven agents investigate an incident around one workload: plan, Kubernetes, observability, changes and history, then "
                   "hypotheses, an independent evidence review and a root-cause analysis. MOKS grades every finding and hypothesis from the evidence.",
    "inputs": [
        {"name": "environment", "label": "Environment", "type": "environment", "required": True},
        {"name": "namespace", "label": "Namespace", "type": "namespace", "required": True, "pattern": r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$", "max": 63},
        {"name": "kind", "label": "Kind", "type": "choice", "choices": ["Deployment", "StatefulSet", "DaemonSet", "CronJob", "Job", "ReplicaSet", "Pod"], "default": "Deployment"},
        {"name": "workload", "label": "Workload", "type": "workload", "required": True, "pattern": r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$", "max": 253},
        {"name": "minutes", "label": "Symptom window (minutes)", "type": "integer", "min": 5, "max": 10080},
        {"name": "change_window", "label": "Change window", "type": "choice", "choices": list(CHANGE_WINDOWS), "default": "24h",
         "help": "How far back to look for changes. Each change is also placed in the smallest of 15m, 1h, 6h and 24h it falls in."},
        {"name": "question", "label": "Incident", "type": "text", "max": 2000, "help": "Symptoms, when they started, who noticed. Optional."},
    ],
}
