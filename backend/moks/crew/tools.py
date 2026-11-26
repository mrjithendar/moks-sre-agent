"""The crew's tools: an allowlist of read-only MOKS tools bound to one target.

The model chooses a tool and a few narrow arguments (a pod, a container, a search phrase). The target,
meaning the person, environment, namespace, workload and time window, is bound here from the
investigation and never taken from the model. A namespace or pod outside the target is refused here and
audited. Everything that is allowed still goes through Investigator._call and the ToolExecutor, so
authorization, the request's scope, timeouts, redaction, truncation, evidence and audit are the same
checks every MOKS tool call passes. There is no exec, apply, patch, delete, scale or Secret read: those
tools are not on the list, and the executor refuses privileged tools whatever asks.

Which pods belong to the workload is decided by Kubernetes owner references, never by name: a Deployment's
pods are those whose controller is one of the ReplicaSets that Deployment (by UID) owns; a CronJob's are those of
the Jobs it owns (by UID); a StatefulSet's, DaemonSet's, Job's or ReplicaSet's are those it controls directly, by
UID; a Pod is only itself. Members holds the result (runner.Run resolves it through
the ToolExecutor). So cart's investigation never reads cart-worker's pods, a pod without owner references
belongs to nothing, and a recreated workload's old pods drop out. When nothing matches, the answer is an
explicit empty result, never the whole namespace.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..tools.contracts import K8S_NAME

METRIC_NAME = r"^[a-zA-Z_:][a-zA-Z0-9_:]{0,200}$"
LABEL = r"^[a-zA-Z_][a-zA-Z0-9_]{0,99}$"
CLUSTER = r"^[a-z0-9][a-z0-9-]{0,99}$"

NAMESPACE_HINT = "Leave empty. The namespace is fixed by MOKS; any other value is refused."


class Narrow(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    namespace: str | None = Field(default=None, max_length=253, description=NAMESPACE_HINT)


class NoArgs(Narrow):
    pass


class PodArgs(Narrow):
    pod: str = Field(pattern=K8S_NAME, max_length=253, description="a pod of the target workload, as listed by workload_pods")


class LogArgs(PodArgs):
    container: str | None = Field(default=None, pattern=K8S_NAME, max_length=63)
    previous: bool = Field(default=False, description="logs of the previous (crashed) container instance")


class EventArgs(Narrow):
    object_name: str | None = Field(default=None, pattern=K8S_NAME, max_length=253, description="the workload or one of its pods; empty for the namespace")


class SearchArgs(Narrow):
    contains: str | None = Field(default=None, max_length=200, description="text to find in the workload's logs")
    errors_only: bool = False


class PathArgs(Narrow):
    path: str | None = Field(default=None, max_length=300, pattern=r"^[A-Za-z0-9_./-]{1,300}$", description="limit to commits touching this path")


class QueryArgs(Narrow):
    query: str = Field(min_length=2, max_length=300, description="what to look for in runbooks and docs")


class SharedSearchArgs(Narrow):
    query: str = Field(default="", max_length=300, description="the symptoms in a sentence")
    errors: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=8,
                                                                description="exact error lines, reasons and codes the evidence shows, e.g. CrashLoopBackOff, exit code 137, ECONNREFUSED")


class MetricArgs(Narrow):
    """One metric in the target's namespace. The namespace label is MOKS's; the model may filter on other labels only."""
    metric: str = Field(pattern=METRIC_NAME, description="a metric name, e.g. rabbitmq_queue_messages_ready or pg_stat_activity_count")
    labels: dict[Annotated[str, Field(pattern=LABEL)], Annotated[str, Field(max_length=200)]] = Field(default_factory=dict, max_length=5,
                                                                                                    description="label -> exact value, or ~regex; never namespace")
    aggregate: Literal["sum", "avg", "max", "min", "count"] = "sum"
    by: list[Annotated[str, Field(pattern=LABEL)]] = Field(default_factory=list, max_length=3)


class TraceArgs(Narrow):
    trace_id: str = Field(pattern=r"^[A-Fa-f0-9]{16,32}$", description="a trace id seen in the workload's logs")


class DashboardArgs(Narrow):
    query: str | None = Field(default=None, max_length=100, description="words from a dashboard title")


class SimilarArgs(Narrow):
    symptoms: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")]] = Field(default_factory=list, max_length=8, description="e.g. crashloop, oomkilled, image_pull")
    signature: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")]] = Field(default_factory=list, max_length=20, description="normalised error words")
    text: str = Field(default="", max_length=300, description="the symptoms in a sentence")
    changes: bool = Field(default=False, description="a rollout or change preceded the problem")


class ClusterArgs(Narrow):
    cluster: str = Field(pattern=CLUSTER, description="a cluster name from datastores")


class EvidenceArgs(Narrow):
    id: str = Field(pattern=r"^E\d{1,4}$", description="an evidence id, e.g. E3")


@dataclass(frozen=True)
class Target:
    environment: str
    namespace: str
    workload: str
    kind: str
    minutes: int
    repository: str  # "" when no single repository is configured
    change_hours: int = 0  # how far back to look for changes; 0: the investigation window


class Refused(Exception):
    """A call outside the target. Audited by the caller, never run."""


class Empty(Exception):
    """The workload has no pods to read for this call: answered as an explicit empty result, never run wider."""


@dataclass(frozen=True)
class Members:
    """The target's verified pods: owners are (kind, name, uid) of the controllers that own them (the Deployment's
    ReplicaSets, or the StatefulSet or DaemonSet itself)."""
    pods: frozenset[str]
    owners: frozenset[tuple[str, str, str | None]]
    reason: str = ""  # why there are none, when there are none
    nodes: frozenset[str] = frozenset()  # the nodes those pods run on

    def regex(self) -> str:
        """An exact alternation of the member pod names, for Loki and Prometheus pod matchers."""
        return "(" + "|".join(sorted(p.replace(".", "[.]") for p in self.pods)) + ")"


NOBODY = Members(frozenset(), frozenset(), "the workload's pods have not been resolved")


def controllers_for(t: Target, workloads: list[dict[str, Any]]) -> tuple[frozenset[tuple[str, str, str | None]], str]:
    """The controllers whose pods are the target's, from k8s_workloads, and why there are none when there are none.
    Not for Deployments (their ReplicaSets come from the rollout history) or Pods (a pod is its own member)."""
    found = next((w for w in workloads if w.get("kind") == t.kind and w.get("name") == t.workload), None)
    if found is None:
        return frozenset(), f"there is no {t.kind} {t.workload} in {t.namespace}"
    if t.kind == "CronJob":
        jobs = frozenset(("Job", str(w.get("name")), w.get("uid")) for w in workloads
                         if w.get("kind") == "Job" and w.get("ownerKind") == "CronJob" and w.get("ownerName") == t.workload
                         and (found.get("uid") is None or w.get("ownerUid") == found.get("uid")))
        return jobs, "" if jobs else "it has no Jobs now (none has run yet, or they were cleaned up)"
    return frozenset({(t.kind, t.workload, found.get("uid"))}), ""


def owned_by(pod: dict[str, Any], owners: frozenset[tuple[str, str, str | None]]) -> bool:
    """A pod summary (summarize_pod) belongs to the target when its controller is one of the owners, matched by UID
    whenever the owner's UID is known. No owner reference, no membership."""
    kind, _, name = str(pod.get("owner") or "").partition("/")
    return any(kind == k and name == n and (uid is None or pod.get("ownerUid") == uid) for k, n, uid in owners)


Binder = Callable[[Target, Any, Members], dict[str, Any]]


@dataclass(frozen=True)
class CrewTool:
    name: str
    tool: str  # the MOKS tool it runs
    agent: str  # its agent in the workload crew; "" for tools only the incident crew offers (crew/incident_config.py)
    description: str
    args: type[Narrow]
    bind: Binder
    needs: str  # connector type, for the gap a missing source leaves


def _pod(t: Target, m: Members, pod: str) -> str:
    if pod not in m.pods:
        raise Refused(f"pod {pod} is not owned by {t.kind.lower()} {t.workload} (by its owner references)")
    return pod


def _object(t: Target, m: Members, name: str | None) -> str | None:
    if name and name != t.workload:
        _pod(t, m, name)
    return name


def _deployment(t: Target) -> str:
    if t.kind != "Deployment":
        raise LookupError(f"{t.kind} has no Deployment rollout history; use the workload status and events instead")
    return t.workload


def _repository(t: Target) -> str:
    if not t.repository:
        raise LookupError("git: no single repository is configured for this environment, so commits cannot be tied to the workload")
    return t.repository


def _cohort(t: Target) -> str:
    """The workload name ML cohorts use: the controller's name, or a Pod's name without its ReplicaSet or ordinal suffix."""
    if t.kind != "Pod":
        return t.workload
    from ..ml.correlate import workload_of

    return workload_of(t.workload)


def _pods(t: Target, m: Members) -> str:
    if not m.pods:
        raise Empty(f"no pods are owned by {t.kind.lower()} {t.workload}" + (f": {m.reason}" if m.reason else ""))
    return m.regex()


def _hours(t: Target) -> int:
    return max(1, t.change_hours or -(-t.minutes // 60))


def _metric(t: Target, a: Any) -> dict[str, Any]:
    fixed = [k for k in (*a.labels, *a.by) if "namespace" in k.lower() or k.startswith("__")]
    if fixed:
        raise Refused(f"label {fixed[0]} is fixed by MOKS to namespace {t.namespace}")
    return {"metric": a.metric, "namespace": t.namespace, "labels": dict(a.labels), "aggregate": a.aggregate, "by": list(a.by), "minutes": max(5, t.minutes)}


def _signals(t: Target, m: Members) -> dict[str, Any]:
    return {"namespace": t.namespace, "workload": t.workload, "pod_regex": m.regex() if m.pods else None, "minutes": max(5, t.minutes)}


NEEDS_MEMBERS = frozenset({"describe_pod", "pod_logs", "recent_events", "search_logs", "pod_metrics", "workload_pods", "namespace_health",
                           "trace_logs", "service_signals", "storage", "services", "node_health"})

TOOLS: tuple[CrewTool, ...] = (
    CrewTool("namespace_health", "k8s_triage", "workload", "Health check of the target namespace with automatic findings (pods, warning events, services, workloads). Start here.",
             NoArgs, lambda t, a, m: {"namespace": t.namespace}, "kubernetes"),
    CrewTool("workload_status", "k8s_workloads", "workload", "Replica health, rollout conditions and images of the target workload.",
             NoArgs, lambda t, a, m: {"namespace": t.namespace}, "kubernetes"),
    CrewTool("workload_pods", "k8s_list_pods", "workload", "The target workload's pods: status, readiness, restarts and problem reasons.",
             NoArgs, lambda t, a, m: {"namespace": t.namespace}, "kubernetes"),
    CrewTool("describe_pod", "k8s_describe_pod", "workload", "One pod of the target workload: containers, images, probes, state, last state, conditions, events.",
             PodArgs, lambda t, a, m: {"namespace": t.namespace, "pod": _pod(t, m, a.pod)}, "kubernetes"),
    CrewTool("recent_events", "k8s_events", "workload", "Kubernetes events (warnings and normal) for the workload, one of its pods, or the namespace.",
             EventArgs, lambda t, a, m: {"namespace": t.namespace, "object_name": _object(t, m, a.object_name), "warnings_only": False}, "kubernetes"),
    CrewTool("pod_logs", "k8s_pod_logs", "workload", "The tail of one pod's container logs, optionally from the previous crashed instance.",
             LogArgs, lambda t, a, m: {"namespace": t.namespace, "pod": _pod(t, m, a.pod), "container": a.container, "previous": a.previous,
                                    "tail_lines": 0, "since_minutes": t.minutes}, "kubernetes"),
    CrewTool("search_logs", "loki_search", "workload", "Search the workload's logs in Loki over the investigation window.",
             SearchArgs, lambda t, a, m: {"namespace": t.namespace, "pod": _pods(t, m), "contains": a.contains, "errors_only": a.errors_only, "limit": 0, "minutes": t.minutes}, "loki"),
    CrewTool("pod_metrics", "prom_pod_resources", "workload", "CPU, memory, memory limit and throttling of the workload's pods (Prometheus).",
             NoArgs, lambda t, a, m: {"namespace": t.namespace, "pod_regex": _pods(t, m), "minutes": max(5, t.minutes)}, "prometheus"),
    CrewTool("firing_alerts", "prom_alerts", "workload", "Prometheus alerts firing within your scope.",
             NoArgs, lambda t, a, m: {"firing_only": True}, "prometheus"),
    CrewTool("ml_signals", "ml_anomalies", "workload", "Shadow-mode ML anomalies for the target workload (baseline, observed value, deviation, model version, "
             "confidence, validation status) and the correlation group it belongs to. Statistical leads to check with live evidence, never findings or causes.",
             NoArgs, lambda t, a, m: {"namespace": t.namespace, "workload": _cohort(t), "minutes": min(1440, max(60, t.minutes))}, "ml"),
    CrewTool("rollout_history", "k8s_rollout_history", "change", "Revisions of the target Deployment with images and creation times.",
             NoArgs, lambda t, a, m: {"namespace": t.namespace, "deployment": _deployment(t)}, "kubernetes"),
    CrewTool("config_audit", "k8s_config_audit", "change", "Configuration of the namespace's workloads: requests, probes, image pinning, missing Secret/ConfigMap references (contents never read).",
             NoArgs, lambda t, a, m: {"namespace": t.namespace}, "kubernetes"),
    CrewTool("recent_commits", "git_recent_commits", "change", "Commits in the environment's repository during the investigation window.",
             PathArgs, lambda t, a, m: {"repository": _repository(t), "path": a.path, "hours": max(1, -(-t.minutes // 60)), "limit": 20}, "git"),
    CrewTool("deployed_commit", "git_compare_deployment", "change", "Ties the running Deployment to its Git commit when its image or annotations carry a SHA.",
             NoArgs, lambda t, a, m: {"namespace": t.namespace, "deployment": _deployment(t), "repository": _repository(t)}, "git"),
    CrewTool("search_runbooks", "kb_search", "change", "Search runbooks, docs and approved postmortems you may read (cited passages).",
             QueryArgs, lambda t, a, m: {"query": a.query, "k": 4}, "knowledge"),
    # Offered only by the incident crew (crew/incident_config.py decides which agent gets which).
    CrewTool("node_health", "k8s_nodes", "", "Readiness, memory/disk/PID pressure, taints and capacity of the nodes the workload's pods run on (cluster-scoped: needs cluster-wide access).",
             NoArgs, lambda t, a, m: {}, "kubernetes"),
    CrewTool("autoscaling", "k8s_autoscaling", "", "HPAs and KEDA ScaledObjects that scale the workload: bounds, current/desired replicas, metrics, stuck at max or unable to scale.",
             NoArgs, lambda t, a, m: {"namespace": t.namespace}, "kubernetes"),
    CrewTool("storage", "k8s_storage", "", "PersistentVolumeClaims the workload's pods mount: phase, capacity, storage class, resize conditions, missing claims.",
             NoArgs, lambda t, a, m: {"namespace": t.namespace}, "kubernetes"),
    CrewTool("services", "k8s_services", "", "Services that select the workload's pods: ports, ready endpoints, selector mismatches (its dependencies' entry points).",
             NoArgs, lambda t, a, m: {"namespace": t.namespace}, "kubernetes"),
    CrewTool("service_signals", "prom_service_signals", "", "The workload's golden signals: throughput, 5xx error ratio and latency p50/p95/p99 (Istio or OpenTelemetry metrics).",
             NoArgs, lambda t, a, m: _signals(t, m), "prometheus"),
    CrewTool("metric", "prom_metric", "", "One Prometheus metric in the workload's namespace (queues, connection pools, caches), aggregated; the namespace is fixed by MOKS.",
             MetricArgs, lambda t, a, m: _metric(t, a), "prometheus"),
    CrewTool("trace_logs", "loki_search", "", "The workload's log lines that carry one trace id (no trace backend is connected: this is how a request is followed).",
             TraceArgs, lambda t, a, m: {"namespace": t.namespace, "pod": _pods(t, m), "trace_id": a.trace_id, "limit": 0, "minutes": t.minutes}, "loki"),
    CrewTool("dashboards", "grafana_dashboards", "", "Grafana dashboards whose title matches, with links that open at the investigation window.",
             DashboardArgs, lambda t, a, m: {"query": a.query, "limit": 10}, "grafana"),
    CrewTool("grafana_alerts", "grafana_alerts", "", "Grafana-managed alert rules firing within your scope.",
             NoArgs, lambda t, a, m: {"firing_only": True}, "grafana"),
    CrewTool("datastores", "datastore_clusters", "", "PostgreSQL and Redis clusters configured here that you may read, with reachability and health findings (their names for the next tools).",
             NoArgs, lambda t, a, m: {"health": True}, "datastore"),
    CrewTool("postgres_health", "pg_overview", "", "One PostgreSQL cluster: connections by state and client against max_connections, long and blocked transactions, replication.",
             ClusterArgs, lambda t, a, m: {"cluster": a.cluster}, "postgres"),
    CrewTool("redis_health", "redis_overview", "", "One Redis/Valkey deployment: memory against maxmemory, clients, evictions, replication and reachability.",
             ClusterArgs, lambda t, a, m: {"cluster": a.cluster}, "redis"),
    CrewTool("gitops_status", "k8s_argocd_status", "", "Argo CD applications that deploy to the workload's namespace: sync status, health, last sync result and revision.",
             NoArgs, lambda t, a, m: {}, "kubernetes"),
    CrewTool("changed_commits", "git_recent_commits", "", "Commits in the change window, optionally touching one path (deploy/, helm/, terraform/, k8s/ for manifest and infrastructure changes).",
             PathArgs, lambda t, a, m: {"repository": _repository(t), "path": a.path, "hours": _hours(t), "limit": 30}, "git"),
    CrewTool("similar_incidents", "similar_incidents", "", "Approved past incidents with similar symptoms on this workload, and how they were resolved. Historical leads, never proof.",
             SimilarArgs, lambda t, a, m: {"symptoms": list(a.symptoms), "services": [t.workload], "signature": list(a.signature), "text": a.text, "changes": a.changes, "k": 5},
             "knowledge"),
    CrewTool("shared_runbooks", "shared_kb_search", "", "The shared SRE knowledge base (moks-rag): runbooks, past postmortems and k3s/Helm/Istio manifests for the "
             "target workload, ranked by the exact error lines and codes you pass. Documentation and history, never the current state.",
             SharedSearchArgs, lambda t, a, m: {"namespace": t.namespace, "workload": t.workload, "errors": list(a.errors), "text": a.query}, "shared_knowledge"),
    CrewTool("incident_history", "incident_history", "change", "Reviewed past incidents of the target workload: recurring failures, repeated infrastructure "
             "problems, runbooks and resolutions that worked. History, not evidence of the current state.",
             NoArgs, lambda t, a, m: {"namespace": t.namespace, "service": _cohort(t), "days": 180}, "knowledge"),
)
BY_NAME = {t.name: t for t in TOOLS}
ALLOWED_MOKS_TOOLS = frozenset(t.tool for t in TOOLS)


def bind(tool: CrewTool, target: Target, raw: dict[str, Any], *, log_lines: int, members: Members = NOBODY) -> dict[str, Any]:
    """The MOKS arguments for a crew call. Raises Refused for anything outside the target, Empty when the workload
    has no pods for it, LookupError for a source that is not configured, and pydantic's ValidationError for
    malformed arguments."""
    parsed = tool.args.model_validate(raw)
    if parsed.namespace and parsed.namespace != target.namespace:
        raise Refused(f"namespace {parsed.namespace} is outside this investigation (namespace {target.namespace})")
    args = tool.bind(target, parsed, members)
    if "tail_lines" in args:
        args["tail_lines"] = log_lines
    if args.get("limit") == 0:
        args["limit"] = log_lines
    return {k: v for k, v in args.items() if v is not None}


def _names(t: Target, m: Members) -> set[str]:
    """Every exact reference to the target: the workload, its controllers and its verified pods, as kind/name in
    the spellings MOKS results use (Pod/x and pod/x)."""
    refs = {f"{t.kind}/{t.workload}", f"{t.kind.lower()}/{t.workload}"}
    refs |= {f"{k}/{n}" for k, n, _ in m.owners} | {f"{k.lower()}/{n}" for k, n, _ in m.owners}
    return refs | {f"Pod/{p}" for p in m.pods} | {f"pod/{p}" for p in m.pods}


def _workload_row(t: Target, w: Any) -> bool:
    return isinstance(w, dict) and w.get("name") == t.workload and w.get("kind", t.kind) == t.kind


def _scales_target(target: Target, scaler: Any) -> bool:
    ref = scaler.get("target") if isinstance(scaler, dict) else None
    return isinstance(ref, dict) and ref.get("name") == target.workload and (ref.get("kind") or target.kind) == target.kind


def focus(target: Target, tool: str, data: Any, members: Members = NOBODY) -> Any:
    """The target's part of a namespace-wide result, for the excerpt. None means the result is about the target
    already (one pod, one Deployment); a namespace-wide result is always narrowed, to an empty one when nothing in
    it is the target's. The stored evidence row keeps everything; this only decides what the model is shown."""
    if tool == "k8s_autoscaling" and isinstance(data, dict):
        return {"hpas": [h for h in data.get("hpas") or [] if _scales_target(target, h)],
                "scaledObjects": None if data.get("scaledObjects") is None else [o for o in data["scaledObjects"] if _scales_target(target, o)],
                "keda": data.get("keda")}
    if tool == "k8s_storage" and isinstance(data, dict):
        claims = [c for c in data.get("claims") or [] if set(c.get("mountedBy") or []) & members.pods]
        return {"claims": claims, "problems": [p for p in data.get("problems") or [] if any(c["name"] in p for c in claims)]}
    if tool == "k8s_services" and isinstance(data, list):
        return [x for x in data if isinstance(x, dict) and (set(x.get("matchingPods") or []) & members.pods or any(n.get("pod") in members.pods for n in x.get("nearMissPods") or []))]
    if tool == "k8s_nodes" and isinstance(data, list):
        return [n for n in data if isinstance(n, dict) and n.get("name") in members.nodes] if members.nodes else data
    if tool == "k8s_argocd_status" and isinstance(data, dict):
        mine_ = lambda r: isinstance(r, dict) and r.get("namespace") == target.namespace  # noqa: E731
        return {"installed": data.get("installed"), "problems": [r for r in data.get("problems") or [] if mine_(r)],
                "findings": [f for f in data.get("findings") or [] if f.get("namespace") == target.namespace], "error": data.get("error")}
    if tool == "k8s_list_pods" and isinstance(data, list):
        return [p for p in data if isinstance(p, dict) and p.get("name") in members.pods]
    if tool == "k8s_workloads" and isinstance(data, list):
        return [w for w in data if _workload_row(target, w)]
    if tool == "k8s_config_audit" and isinstance(data, dict):
        return {**data, "workloads": [w for w in data.get("workloads") or [] if str(w.get("workload") or "") in _names(target, members)]}
    if tool == "k8s_triage" and isinstance(data, dict):
        refs = _names(target, members)
        return {"findings": [f for f in data.get("findings") or [] if mine(target, f, members)],
                "unhealthyPods": [p for p in data.get("unhealthyPods") or [] if p.get("name") in members.pods],
                "workloads": [w for w in data.get("workloads") or [] if _workload_row(target, w)],
                "warningEvents": [e for e in data.get("warningEvents") or [] if str(e.get("object") or "") in refs]}
    return None


def mine(target: Target, finding: dict[str, Any], members: Members = NOBODY) -> bool:
    refs = _names(target, members)
    return any(str(r) in refs for r in finding.get("resources") or []) or str(finding.get("controller") or finding.get("owner") or "") in refs
