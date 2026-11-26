"""Runtime settings: the deployment values an administrator may change while MOKS runs (docs/SETTINGS.md).

Helm sets every MOKS_* value at start-up. A row in runtime_settings overrides one of the keys in SPECS, inside the
bounds declared there. A limit (a value that caps cost or load) can only be tightened: its Helm value is the most it
may be set to, so loosening one stays a reviewed Helm change. Switches and schedules move freely inside their bounds.
Saving the Helm value removes the row.

Every process puts the rows on its own Settings object whenever it reports its build (versions.report, about every
30 seconds), and the API also does so right after a save. Code keeps reading get_settings() and needs no database
session. A row that no longer fits, for example after Helm lowered a ceiling, is ignored and the Helm value applies.

Nothing secret is editable or shown: secrets come only from the environment, and the deployment view says only
whether each one is set.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit
from .config import Settings, get_settings
from .models import RuntimeSetting, User

log = logging.getLogger("moks.settings")

APPLY_SECONDS = 30  # another process applies a change when it next reports its build

Kind = Literal["int", "float", "bool"]


@dataclass(frozen=True)
class Spec:
    key: str           # the Settings attribute; the environment variable is MOKS_<KEY>
    group: str
    label: str
    help: str
    kind: Kind
    min: float | None = None
    max: float | None = None
    limit: bool = False      # caps cost or load, so the Helm value is also a ceiling
    optional: bool = False   # may be empty, meaning no limit
    unit: str = ""
    helm: str = ""           # the values.yaml path, when the chart exposes one


GROUPS = {"investigations": "Investigations", "llm": "LLM spend", "ml": "Machine learning", "crewai": "CrewAI", "knowledge": "Knowledge search"}

SPECS: dict[str, Spec] = {s.key: s for s in (
    Spec("auto_investigations_per_hour", "investigations", "Automatic investigations per hour",
         "Alert- and detection-triggered investigations per environment per hour, counted for each trigger. Beyond it the incident still opens and says the run was skipped.",
         "int", 1, 120, limit=True, unit="per hour", helm="config.autoInvestigationsPerHour"),
    Spec("knowledge_max_searches", "investigations", "Knowledge searches per investigation",
         "Runbook searches one investigation may run: the first, plus refreshes after triage. They come from this allowance, never from live evidence calls.",
         "int", 1, 6, limit=True, helm="config.knowledgeMaxSearches"),
    Spec("intent_llm_classifier", "investigations", "Ask the chat model when a question is unclear",
         "When the rules are unsure what a question asks for, one short chat-model call decides. Off: the rules' best guess is used.", "bool"),
    Spec("llm_user_daily_budget_usd", "llm", "Daily LLM budget per person",
         "Model spend one person may cause per UTC day, across every provider. Empty: no per-person limit; each provider's own daily budget still applies.",
         "float", 0.01, 10000, limit=True, optional=True, unit="USD"),
    Spec("llm_investigation_budget_usd", "llm", "LLM budget per investigation",
         "Model spend one investigation may cause, follow-up questions included. Empty: no per-investigation limit.",
         "float", 0.01, 10000, limit=True, optional=True, unit="USD"),
    Spec("ml_anomaly_interval_minutes", "ml", "Anomaly scoring interval",
         "How often the worker scores each environment that has an active anomaly model. Nothing is scored while anomaly detection is disabled.",
         "int", 5, 240, unit="minutes", helm="ml.anomaly.intervalMinutes"),
    Spec("ml_similar_incidents_k", "ml", "Similar incidents shown",
         "How many similar past incidents an investigation lists, from approved postmortems.", "int", 1, 10, helm="ml.similarIncidents.k"),
    Spec("ml_learning_capture", "ml", "Capture learning records",
         "Write a learning record when an investigation finishes. Capturing is not training: nothing is learned until someone reviews records and builds a dataset.",
         "bool", helm="ml.learningCapture"),
    Spec("ml_learning_retention_days", "ml", "Keep learning records",
         "Days a new learning record is kept before the daily purge deletes it; records already written keep their expiry. 0 keeps them until deleted.",
         "int", 0, 3650, unit="days", helm="ml.learningRetentionDays"),
    Spec("crewai_timeout_seconds", "crewai", "Crew run timeout", "The longest one CrewAI run may take before it stops and reports what it has.",
         "int", 10, 900, limit=True, unit="seconds", helm="crewai.timeoutSeconds"),
    Spec("crewai_max_iterations", "crewai", "Iterations per agent", "Reasoning rounds each agent in a crew may take.",
         "int", 1, 20, limit=True, helm="crewai.maxIterations"),
    Spec("crewai_max_tool_calls", "crewai", "Tool calls per crew run",
         "Across the whole crew, and never more than the investigation ceiling (MOKS_MAX_TOOL_CALLS).", "int", 1, 100, limit=True, helm="crewai.maxToolCalls"),
    Spec("rag_query_expansion", "knowledge", "Expand questions with SRE synonyms",
         "Adds related terms (restarting: CrashLoopBackOff, OOMKilled; slow: latency, p99) as one more keyword ranking. The question itself, its identifiers and its vector never change.",
         "bool"),
    Spec("rag_require_embedding_evaluation", "knowledge", "Evaluate a new embedding model before it replaces the current one",
         "A rebuild with a different embedding model starts only after that model passed the embedding evaluation against the one serving search. Off: any model can replace it.",
         "bool"),
    Spec("rag_weight_keyword", "knowledge", "Keyword ranking weight",
         "How much the full-text ranking counts when rankings are fused (reciprocal rank fusion). 0 leaves it out.", "float", 0, 5),
    Spec("rag_weight_vector", "knowledge", "Vector ranking weight",
         "How much the embedding (semantic) ranking counts when rankings are fused. 0 leaves it out.", "float", 0, 5),
    Spec("rag_weight_identifier", "knowledge", "Exact identifier weight",
         "How much an exact match on an error code, resource name, path or setting counts. 2 is what search did before this was a setting.", "float", 0, 5),
    Spec("rag_weight_entity", "knowledge", "Named service weight",
         "How much a search for the services a question names counts, when they are among the services you can read about. 0 leaves it out.", "float", 0, 5),
    Spec("rag_query_signals", "knowledge", "Read intent, services and time from questions",
         "Small ranking signals from what a question names: the kind of answer (runbook, past incident, alert), services, environments and a time window. Never a filter.", "bool"),
    Spec("rag_rerank_min_score", "knowledge", "Reranker score floor",
         "With the reranker on, passages it scores below this are dropped; an exact identifier match never is. Empty: nothing is dropped.",
         "float", -20, 20, optional=True),
    Spec("rag_rerank_interactive", "knowledge", "Rerank on the Knowledge search page",
         "Off: only investigations wait for the reranker, and the search page answers without it.", "bool"),
    Spec("rag_structured_chunking", "knowledge", "Split documents by their structure",
         "Alert rules per rule, manifests per resource, OpenAPI per operation, dashboards per panel, logs per event. Changing it re-reads every document at its next sync.", "bool"),
    Spec("rag_index_investigations", "knowledge", "Search past investigations",
         "Finished investigations become searchable history, readable only by those who may read the investigation. Off: the history is removed at the next sync.", "bool"),
    Spec("rag_history_days", "knowledge", "Investigation history reaches back", "Older investigations leave the history.", "int", 7, 3650, unit="days"),
    Spec("shared_rag_enabled", "knowledge", "Search the shared SRE knowledge base (moks-rag)",
         "Alert triage and root-cause analysis also search moks-rag's runbooks, postmortems and manifests (RAG_API_URL, RAG_BEARER_TOKEN). "
         "Off: nothing is sent to moks-rag. It has no effect while those two are not set.", "bool"),
    Spec("shared_rag_top_k", "knowledge", "Shared knowledge passages per search",
         "How many passages one moks-rag search asks for. Empty: rag-api's own default.", "int", 1, 50, optional=True),
)}

_helm: dict[str, Any] = {}     # each key's value before this process first overrode it
_applied: dict[str, Any] = {}  # what this process put on its Settings from a row


class Invalid(ValueError):
    """The value cannot be saved; the message says why, for the person who typed it."""


def helm_value(key: str) -> Any:
    return _helm[key] if key in _helm else getattr(get_settings(), key)


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def bounds(spec: Spec) -> tuple[float | None, float | None]:
    """The range a value may take now. A limit's ceiling is its Helm value when that is lower than the code's."""
    hi = spec.max
    helm = helm_value(spec.key)
    if spec.limit and helm is not None:
        hi = helm if hi is None else min(hi, helm)
    return spec.min, hi


def check(spec: Spec, raw: Any) -> Any:
    """The value to store, or Invalid."""
    helm = helm_value(spec.key)
    if raw is None or raw == "":
        if not spec.optional:
            raise Invalid(f"{spec.label} needs a value.")
        if spec.limit and helm is not None:
            raise Invalid(f"Helm limits this to {_fmt(helm)}{' ' + spec.unit if spec.unit else ''}. Removing the limit is a Helm change.")
        return None
    if spec.kind == "bool":
        if not isinstance(raw, bool):
            raise Invalid(f"{spec.label} is on or off.")
        return raw
    if isinstance(raw, bool):
        raise Invalid(f"{spec.label} must be a number.")
    try:
        num = float(raw)
    except (TypeError, ValueError):
        raise Invalid(f"{spec.label} must be a number.") from None
    if not math.isfinite(num):
        raise Invalid(f"{spec.label} must be a number.")
    if spec.kind == "int":
        if not num.is_integer():
            raise Invalid(f"{spec.label} must be a whole number.")
        value: int | float = int(num)
    else:
        value = round(num, 2)
    lo, hi = bounds(spec)
    if lo is not None and value < lo:
        raise Invalid(f"{spec.label} must be at least {_fmt(lo)}.")
    if hi is not None and value > hi:
        if spec.limit and helm is not None and hi == helm:
            raise Invalid(f"Helm sets this limit to {_fmt(hi)}. It can be lowered here, not raised: raising it is a Helm change.")
        raise Invalid(f"{spec.label} must be at most {_fmt(hi)}.")
    return value


def apply(values: dict[str, Any]) -> dict[str, str]:
    """Puts these values on this process's Settings; a key that has none (or an invalid one) gets its Helm value back.
    Returns why each ignored value was ignored."""
    s = get_settings()
    ignored: dict[str, str] = {}
    for key, spec in SPECS.items():
        ok = False
        if key in values:
            try:
                value, ok = check(spec, values[key]), True
            except Invalid as exc:
                ignored[key] = str(exc)
        if ok:
            _helm.setdefault(key, getattr(s, key))
            setattr(s, key, value)
            _applied[key] = value
        elif key in _applied:
            setattr(s, key, _helm[key])
            del _applied[key]
    for key, why in ignored.items():
        log.warning("runtime setting ignored", extra={"event": "runtime_setting_ignored", "operation": key, "error": why})
    return ignored


async def _values(db: AsyncSession) -> dict[str, Any]:
    return {r.key: (r.value or {}).get("value") for r in (await db.execute(select(RuntimeSetting))).scalars().all()}


async def refresh(db: AsyncSession | None = None) -> None:
    """Applies what the database holds now. If it cannot be read, this process keeps what it applied last."""
    try:
        if db is not None:
            values = await _values(db)
        else:
            from .db import session_scope

            async with session_scope() as own:
                values = await _values(own)
    except Exception as exc:
        log.warning("runtime settings unreadable", extra={"event": "runtime_settings_unreadable", "errorType": type(exc).__name__})
        return
    apply(values)


async def describe(db: AsyncSession) -> list[dict[str, Any]]:
    """Every editable setting with its effective value, Helm value, bounds and who changed it last."""
    rows = {r.key: r for r in (await db.execute(select(RuntimeSetting))).scalars().all()}
    ids = {r.updated_by for r in rows.values() if r.updated_by}
    emails = dict((await db.execute(select(User.id, User.email).where(User.id.in_(ids)))).all()) if ids else {}
    ignored = apply({k: (r.value or {}).get("value") for k, r in rows.items()})
    out = []
    for key, spec in SPECS.items():
        r = rows.get(key)
        live = r is not None and key not in ignored
        lo, hi = bounds(spec)
        out.append({"key": key, "group": spec.group, "group_label": GROUPS[spec.group], "label": spec.label, "help": spec.help, "kind": spec.kind,
                    "unit": spec.unit, "optional": spec.optional, "limit": spec.limit, "min": lo, "max": hi,
                    "value": (r.value or {}).get("value") if live else helm_value(key), "helm_value": helm_value(key), "overridden": live,
                    "ignored": ignored.get(key), "env": f"MOKS_{key.upper()}", "helm": spec.helm,
                    "reason": r.reason if r else None, "updated_at": r.updated_at.isoformat() if r and r.updated_at else None,
                    "updated_by": emails.get(r.updated_by) if r else None})
    return out


async def save(db: AsyncSession, key: str, raw: Any, reason: str, user: User) -> None:
    """Validates, stores (or removes, when it equals Helm), audits, and applies to this process."""
    spec = SPECS[key]
    value = check(spec, raw)
    row = await db.get(RuntimeSetting, key)
    before = (row.value or {}).get("value") if row else helm_value(key)
    helm = helm_value(key)
    if value == helm and type(value) is type(helm):
        if row is not None:
            await db.delete(row)
    else:
        if row is None:
            row = RuntimeSetting(key=key)
            db.add(row)
        row.value, row.reason, row.updated_by, row.updated_at = {"value": value}, reason, user.id, datetime.now(timezone.utc)
    await audit.record(db, action="settings.update", actor_id=user.id, actor_email=user.email, resource_type="runtime_setting", resource_id=key, decision="allow",
                       details={"from": before, "to": value, "helm": helm, "reason": reason})
    await db.flush()
    await refresh(db)


async def reset(db: AsyncSession, key: str, reason: str, user: User) -> None:
    """Back to the Helm value."""
    row = await db.get(RuntimeSetting, key)
    if row is None:
        return
    before = (row.value or {}).get("value")
    await db.delete(row)
    await audit.record(db, action="settings.reset", actor_id=user.id, actor_email=user.email, resource_type="runtime_setting", resource_id=key, decision="allow",
                       details={"from": before, "to": helm_value(key), "reason": reason})
    await db.flush()
    await refresh(db)


# ---- the deployment's configuration, read-only ------------------------------------------------------------------

SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Authentication and sessions", ("auth_", "oidc_", "session_", "cookie_", "bootstrap_")),
    ("Investigations", ("max_", "default_query", "investigation_", "intent_", "knowledge_", "guidance_", "evidence_", "auto_investigations")),
    ("LLM spend", ("llm_",)),
    ("Machine learning", ("ml_",)),
    ("Knowledge search", ("rag_", "pinecone_", "shared_rag_")),
    ("CrewAI", ("crewai_",)),
    ("Secrets sync", ("secrets_",)),
    ("Terminal", ("terminal_",)),
    ("Workers and jobs", ("worker_", "job_", "embedded_worker")),
    ("Rate limits", ("rate_",)),
    ("Backups", ("database_backup", "database_volume", "velero_")),
    ("Alerts", ("alert_",)),
    ("Observability", ("metrics_port", "log_level", "otel_", "trace_")),
)
NOTES = {"Terminal": "The terminal gateway gets its own terminal.* values from Helm; this is the API's copy, which may differ.",
         "Secrets sync": "The executor gets its own secretsSync.* values from Helm; this is the API's copy, which may differ."}
SECRET_KEYS = {"database_url"}  # holds a password, so it is treated like the fields marked repr=False
_USERINFO = re.compile(r"(://)[^/@\s]+@")


def _section(key: str) -> str:
    for name, prefixes in SECTIONS:
        if key.startswith(prefixes):
            return name
    return "Platform"


def deployment() -> list[dict[str, Any]]:
    """Every MOKS_* value this process started with, grouped. Secrets say only whether they are set."""
    s = get_settings()
    groups: dict[str, list[dict[str, Any]]] = {}
    for key, field in Settings.model_fields.items():
        alias = getattr(field.validation_alias, "choices", None)  # read under another name, e.g. RAG_API_URL for moks-rag
        item: dict[str, Any] = {"key": key, "env": str(alias[0]) if alias else f"MOKS_{key.upper()}", "editable": key in SPECS}
        value = helm_value(key) if key in SPECS else getattr(s, key)
        if not field.repr or key in SECRET_KEYS:
            item.update(secret=True, set=bool(value))
        else:
            item.update(secret=False, value=_USERINFO.sub(r"\1***@", value) if isinstance(value, str) else value)
        groups.setdefault(_section(key), []).append(item)
    order = [name for name, _ in SECTIONS] + ["Platform"]
    return [{"section": name, "note": NOTES.get(name), "items": groups[name]} for name in order if name in groups]
