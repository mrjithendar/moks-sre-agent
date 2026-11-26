"""The evidence contract every investigation engine and every agent shares (docs/UNIFIED_INVESTIGATION.md#evidence-contract).

An EvidenceItem is a view of one stored Evidence row: the result the ToolExecutor recorded after checking the
person's access, redacting and truncating it. It is built only from that row and the investigation's own scope,
never from anything a model wrote, so two engines that read the same thing describe it the same way. Every read
in MOKS passes the executor, which makes this the one place the contract can be enforced: LangGraph, the workload
crew and the unified pipeline all produce it, and `GET /investigations/{id}/evidence-items` serves it for any of them.

Confidence here is about the evidence, not about a hypothesis: how far this record can be relied on to describe
what it read (the source's reliability, whether the read was complete, whether it is still fresh). It is a stated
prior, not a calibrated probability, and the basis is listed with it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..orchestrator import evidence as ledger
from ..redaction import redact

CONTRACT = "moks.evidence/v1"

SourceType = Literal["kubernetes", "metrics", "logs", "traces", "deployment", "git", "configuration", "alert", "ml_anomaly", "knowledge",
                     "historical_incident", "dependency", "cost", "datastore", "policy", "other"]

# A tool's source type, by exact name first, then by prefix. Trace correlation through logs is a trace read.
_EXACT: dict[str, str] = {
    "k8s_pod_logs": "logs", "k8s_rollout_history": "deployment", "k8s_argocd_status": "deployment", "git_compare_deployment": "deployment",
    "k8s_config_audit": "configuration", "k8s_services": "dependency", "k8s_ingresses": "dependency", "k8s_dns_health": "dependency",
    "prom_alerts": "alert", "grafana_alerts": "alert", "cw_alarms": "alert", "kb_search": "knowledge", "shared_kb_search": "knowledge", "similar_incidents": "historical_incident",
    "ml_observations": "ml_anomaly", "ml_anomalies": "ml_anomaly", "prom_targets": "metrics", "cw_logs_query": "logs", "cw_log_groups": "logs", "cw_metric": "metrics", "cw_metrics_list": "metrics",
}
_PREFIX: tuple[tuple[str, str], ...] = (("trace_", "traces"), ("k8s_", "kubernetes"), ("eks_", "kubernetes"), ("prom_", "metrics"), ("grafana_", "metrics"),
                                        ("loki_", "logs"), ("git_", "git"), ("finops_", "cost"), ("pg_", "datastore"), ("redis_", "datastore"), ("datastore_", "datastore"))

# How far each kind of source can be relied on to describe what it read. Stated priors, shown with every item and in
# the docs, so a reader can disagree with them; they weight evidence, they are not probabilities.
RELIABILITY: dict[str, tuple[float, str]] = {
    "kubernetes": (0.95, "the API server's own record of current state"),
    "deployment": (0.95, "rollout and sync records kept by Kubernetes or Argo CD"),
    "configuration": (0.9, "the pod specs as the API server returns them"),
    "dependency": (0.85, "Services, endpoints and DNS as the cluster reports them"),
    "metrics": (0.85, "sampled time series; aggregation and scrape gaps can hide short events"),
    "datastore": (0.85, "a datastore's own statistics"),
    "git": (0.8, "commit metadata; a commit is not necessarily deployed"),
    "logs": (0.75, "what the application chose to write; lines can be missing, sampled or misleading"),
    "traces": (0.75, "span records, or trace ids correlated through logs, which see only what was logged"),
    "alert": (0.7, "alert rules fire on thresholds someone chose; they can be noisy or stale"),
    "cost": (0.6, "billing data, delayed and estimated"),
    "ml_anomaly": (0.55, "a model's view that a window is unusual for this workload, never why"),
    "historical_incident": (0.3, "a past incident: a lead, never evidence of the current cause"),
    "knowledge": (0.3, "documentation: what someone wrote down, not the state of the system"),
    "policy": (0.0, "a refusal: shows what could not be read"),
    "other": (0.5, "a source MOKS has no stated reliability for"),
}
SHADOW_RELIABILITY = (0.35, "a shadow-mode model: its observations are being judged and are not trusted yet")

# Content written by applications, people or third parties. It can carry instructions and is never one.
UNTRUSTED_TYPES = {"logs", "traces", "git", "knowledge", "historical_incident", "kubernetes", "alert", "configuration"}
WORKLOAD_OF_POD = re.compile(r"^(.+?)(-[a-z0-9]{6,10}-[a-z0-9]{5}|-[0-9]+)$")


def source_type(tool: str, args: dict[str, Any] | None = None) -> str:
    if tool == "loki_search" and (args or {}).get("trace_id"):
        return "traces"
    if tool in _EXACT:
        return _EXACT[tool]
    return next((t for p, t in _PREFIX if tool.startswith(p)), "other")


def workload_of(pod: str) -> str:
    m = WORKLOAD_OF_POD.match(pod or "")
    return m.group(1) if m else pod


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tool: str
    connector_type: str | None = None
    connector_id: str | None = None
    query: Any = None  # the exact query or API path the tool reports, never a paraphrase
    args: dict[str, Any] = Field(default_factory=dict)


class Resource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    uri: str  # k8s://<env>/<namespace>[/<Kind>/<name>], git://<repo>[/<path>], knowledge://<env>[/<namespace>], ml://<env>/<namespace>[/<service>]
    kind: str | None = None
    name: str | None = None
    namespace: str | None = None


class TimeRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    start: str | None = None
    end: str | None = None


class RawRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    uri: str  # moks://investigations/<id>/evidence/<ref>
    api: str  # the authorized route that returns the stored row


class Confidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    score: float = Field(ge=0, le=1)
    level: Literal["high", "medium", "low", "none"]
    basis: list[str] = Field(default_factory=list)


class Access(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    principal_id: str | None
    permission: str = "investigation.run"
    environment: str
    scope: str  # namespace:<ns> | cluster | knowledge:<env>/<ns or *>
    decision: Literal["allow", "deny"]
    reason: str | None = None
    redacted: bool = True  # the executor redacts every result before it is stored
    truncated: bool = False
    untrusted_content: bool = False
    realm: str = "production"


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    contract: str = CONTRACT
    id: str = Field(pattern=r"^E\d{1,5}$")
    investigation_id: str
    source_type: SourceType
    source_ref: SourceRef
    resource: Resource
    service: str | None = None
    environment: str
    namespace: str | None = None
    timestamp: str | None  # when MOKS collected it
    time_range: TimeRange
    observation: str  # the tool's own summary of the stored, redacted result; never model text
    raw_ref: RawRef
    confidence: Confidence
    access: Access
    status: Literal["ok", "partial", "failed", "denied"]
    kind: str  # orchestrator.evidence kind: observation, incomplete, tool_error, access_denied, documentation, historical
    signals: list[str] = Field(default_factory=list)  # what the pipeline read from it (signals.py), by key
    stage: str | None = None
    collected_by: str | None = None

    def brief(self) -> str:
        """One line for an agent's ledger listing."""
        when = f"{self.time_range.start or '?'}..{self.time_range.end or '?'}"
        return (f"{self.id} [{self.source_type}/{self.status}, confidence {self.confidence.level} {self.confidence.score:.2f}] {self.resource.uri}"
                f"{' service=' + self.service if self.service else ''} window={when}: {self.observation[:240]}")


@dataclass
class Scope:
    """What an adapter knows about the investigation: never anything the evidence itself claims."""
    investigation_id: str
    environment: str
    principal_id: str | None
    time_from: datetime
    time_to: datetime
    realm: str = "production"
    connector_ids: dict[str, str | None] = field(default_factory=dict)
    target: Any = None  # pipeline.Target when the pipeline knows which workload a call was about
    max_age_seconds: int = ledger.DEFAULT_MAX_AGE_SECONDS
    stages: dict[str, str] = field(default_factory=dict)  # ref -> stage that asked for it
    agents: dict[str, str] = field(default_factory=dict)  # ref -> agent or component that asked for it


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()
    return str(value)


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _window(args: dict[str, Any], start: datetime, end: datetime) -> TimeRange:
    """The part of the investigation window the call read: `minutes` narrows it from the end, `hours` reaches back."""
    if isinstance(args.get("minutes"), int) and args["minutes"] > 0:
        start = max(start, end - timedelta(minutes=args["minutes"]))
    elif isinstance(args.get("hours"), int) and args["hours"] > 0:
        start = end - timedelta(hours=args["hours"])
    return TimeRange(start=start.isoformat(), end=end.isoformat())


def _resource(tool: str, args: dict[str, Any], environment: str, stype: str, target: Any) -> Resource:
    ns = args.get("namespace") if isinstance(args.get("namespace"), str) else None
    base = f"k8s://{environment}/{ns}" if ns else f"k8s://{environment}"
    if stype == "knowledge" or stype == "historical_incident":
        scope_ns = getattr(target, "namespace", None) or ns
        return Resource(uri=f"knowledge://{environment}" + (f"/{scope_ns}" if scope_ns else ""), kind="Knowledge", namespace=scope_ns)
    if stype == "ml_anomaly":
        svc = args.get("service")
        return Resource(uri=f"ml://{environment}/{ns}" + (f"/{svc}" if svc else ""), kind="Workload" if svc else "Namespace", name=svc, namespace=ns)
    if args.get("repository"):
        path = args.get("path")
        return Resource(uri=f"git://{args['repository']}" + (f"/{path}" if path else ""), kind="Repository", name=str(args["repository"]))
    for key, kind in (("pod", "Pod"), ("deployment", "Deployment"), ("object_name", None)):
        name = args.get(key)
        if isinstance(name, str) and name and not any(c in name for c in "|()*[]^$"):
            if kind is None:
                kind = getattr(target, "kind", None) if target is not None and name == getattr(target, "workload", None) else "Pod"
            return Resource(uri=f"{base}/{kind}/{name}", kind=kind, name=name, namespace=ns)
    if target is not None and ns and ns == getattr(target, "namespace", None) and getattr(target, "workload", None):
        # a namespace-wide read made for one target is narrowed to it before anything uses it (signals.py)
        return Resource(uri=f"{base}/{target.kind}/{target.workload}", kind=target.kind, name=target.workload, namespace=ns)
    return Resource(uri=base, kind="Namespace" if ns else "Cluster", name=ns, namespace=ns)


def _service(resource: Resource, target: Any, args: dict[str, Any]) -> str | None:
    if target is not None and getattr(target, "service", None) and resource.namespace == getattr(target, "namespace", None):
        if resource.name in (None, getattr(target, "workload", None)) or resource.name in getattr(target, "pods", ()):
            return target.service
    if isinstance(args.get("service"), str):
        return args["service"]
    if resource.kind == "Pod" and resource.name:
        return workload_of(resource.name)
    if resource.kind in ("Deployment", "StatefulSet", "DaemonSet", "Workload") and resource.name:
        return resource.name
    return None


def confidence_of(stype: str, kind: str, ok: bool, truncated: bool, stale: bool, shadow: bool = False) -> Confidence:
    if not ok:
        return Confidence(score=0.0, level="none", basis=["the read failed or was refused: it shows only that the source could not be read"])
    base, why = SHADOW_RELIABILITY if shadow else RELIABILITY.get(stype, RELIABILITY["other"])
    basis = [f"source {stype}: {base:.2f} ({why})"]
    score = base
    if truncated or kind == ledger.INCOMPLETE:
        score *= 0.7
        basis.append("incomplete or truncated result: x0.70")
    if stale or kind == ledger.HISTORICAL:
        score *= 0.75
        basis.append("collected earlier than the freshness limit: x0.75")
    score = round(score, 2)
    return Confidence(score=score, level="high" if score >= 0.8 else "medium" if score >= 0.5 else "low" if score > 0 else "none", basis=basis)


def build(scope: Scope, *, ref: str, tool: str, args: dict[str, Any] | None, ok: bool, kind: str | None, summary: str | None, error: str | None,
          denied: bool, truncated: bool, collected_at: Any, source: dict[str, Any] | None, connector_type: str | None = None,
          connector_id: str | None = None, data: Any = None, signals: list[str] | None = None, now: datetime | None = None) -> EvidenceItem:
    """The contract for one stored result. Everything comes from the row and the investigation's own scope."""
    args = args if isinstance(args, dict) else {}
    stype = source_type(tool, args)
    kind = kind or ledger.classify(ok=ok, tool=tool, denied=denied, truncated=truncated)
    collected = _when(collected_at)
    stale = collected is not None and ((now or datetime.now(timezone.utc)) - collected).total_seconds() > scope.max_age_seconds and stype not in ("knowledge", "historical_incident")
    shadow = stype == "ml_anomaly" and isinstance(data, list) and any(isinstance(o, dict) and o.get("mode") == "shadow" for o in data)
    resource = _resource(tool, args, scope.environment, stype, scope.target)
    src = source or {}
    status = "denied" if denied else "failed" if not ok else "partial" if truncated or kind == ledger.INCOMPLETE else "ok"
    access_scope = (f"knowledge:{scope.environment}/{resource.namespace or '*'}" if stype in ("knowledge", "historical_incident")
                    else f"namespace:{resource.namespace}" if resource.namespace else "cluster")
    observation = redact(str(summary or error or ""))
    if not ok and error and error not in observation:
        observation = f"{observation} ({redact(str(error))[:200]})"
    return EvidenceItem(
        id=ref, investigation_id=scope.investigation_id, source_type=stype,  # type: ignore[arg-type]
        source_ref=SourceRef(tool=tool, connector_type=connector_type, connector_id=connector_id or scope.connector_ids.get(connector_type or ""),
                             query=src.get("query") or src.get("queries") or src.get("api"), args=args),
        resource=resource, service=_service(resource, scope.target, args), environment=scope.environment, namespace=resource.namespace,
        timestamp=_iso(collected_at), time_range=_window(args, scope.time_from, scope.time_to), observation=observation[:600],
        raw_ref=RawRef(uri=f"moks://investigations/{scope.investigation_id}/evidence/{ref}", api=f"/investigations/{scope.investigation_id}/evidence/{ref}"),
        confidence=confidence_of(stype, kind, ok, truncated, stale, shadow),
        access=Access(principal_id=scope.principal_id, environment=scope.environment, scope=access_scope, decision="deny" if denied else "allow",
                      reason=redact(str(error))[:300] if denied and error else None, truncated=bool(truncated), untrusted_content=stype in UNTRUSTED_TYPES, realm=scope.realm),
        status=status, kind=kind, signals=sorted(set(signals or [])), stage=scope.stages.get(ref), collected_by=scope.agents.get(ref))


def from_record(record: dict[str, Any], scope: Scope, *, signals: list[str] | None = None, now: datetime | None = None) -> EvidenceItem:
    """From an Investigator.evidence entry (what _call keeps after the executor stored the row)."""
    from ..tools.builtin import REGISTRY

    spec = REGISTRY.get(record["tool"])
    return build(scope, ref=record["ref"], tool=record["tool"], args=record.get("args"), ok=bool(record.get("ok")), kind=record.get("kind"), summary=record.get("summary"),
                 error=record.get("error"), denied=bool(record.get("denied")), truncated=bool(record.get("truncated")), collected_at=record.get("collected_at"),
                 source=record.get("source"), connector_type="policy" if record.get("denied") else spec.connector_type if spec else None, data=record.get("data"),
                 signals=signals, now=now)


def from_row(row: Any, scope: Scope, *, signals: list[str] | None = None, now: datetime | None = None) -> EvidenceItem:
    """From a stored Evidence row, for the API: the same contract whichever engine collected it."""
    return build(scope, ref=row.ref, tool=row.tool, args=row.args, ok=bool(row.ok), kind=row.kind, summary=row.summary, error=row.error, denied=row.kind == ledger.ACCESS_DENIED,
                 truncated=bool(row.truncated), collected_at=row.collected_at, source=row.source, connector_type=row.connector_type,
                 connector_id=str(row.connector_id) if row.connector_id else None, data=row.data, signals=signals, now=now)


def schema() -> dict[str, Any]:
    """The JSON Schema of the contract, published at GET /intelligence/evidence-schema."""
    return {"$id": CONTRACT, **EvidenceItem.model_json_schema()}
