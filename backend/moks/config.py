"""Process configuration, read once from the environment (prefix MOKS_).

Only deployment-level settings live here. Connectors, LLM providers, environments and
role bindings are application data in PostgreSQL, managed through the admin API. Secrets
are never stored in the database in clear: rows hold *references*, and a pasted value is stored encrypted
(moks/credentials.py, docs/CREDENTIALS.md).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOKS_", env_file=None, extra="ignore")

    env: Literal["development", "test", "production"] = "production"

    # Public URL handling. Everything (SPA, API, docs, auth callbacks) is served under
    # base_path by the app itself; the ingress must NOT strip the prefix.
    public_url: str = "https://jithendar.com/moks"
    base_path: str = "/moks"

    database_url: str = "postgresql+asyncpg://moks@localhost:5432/moks"
    database_pool_size: int = 5

    # Authentication: oidc | local | dev. dev is refused unless env=development.
    auth_mode: Literal["oidc", "local", "dev"] = "oidc"
    session_secret: str = Field(default="", repr=False)
    # Encrypts credentials typed into Settings (moks/credentials.py). Empty: derived from session_secret, so changing
    # that secret then needs the old value here as credentials_key_previous until Settings re-encrypts them.
    credentials_key: str = Field(default="", repr=False)
    credentials_key_previous: str = Field(default="", repr=False)
    # At start-up the API copies every MOKS_CRED_* value that a connector, LLM provider, knowledge source or backup
    # storage names into stored credentials and points the reference at the copy, so nothing keeps reading the
    # Kubernetes Secret. Off: references keep reading the environment until someone imports them in Settings.
    credentials_import_on_start: bool = True
    session_idle_minutes: int = 480
    session_absolute_hours: int = 12
    cookie_secure: bool = True

    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = Field(default="", repr=False)
    oidc_scopes: str = "openid email profile"
    # Emails granted Administrator on first sign-in (comma separated). Everyone else starts
    # with no role bindings and therefore no access until an administrator grants one.
    bootstrap_admins: str = ""
    # Optional JSON map of IdP group -> list of "role:environment:namespace" grants, applied at sign-in.
    oidc_group_roles: str = "{}"
    oidc_groups_claim: str = "groups"
    # Restrict sign-in to these email domains (comma separated); empty allows any verified email.
    oidc_allowed_domains: str = ""

    # API docs at {base_path}/api/docs. Disabled by default in production.
    api_docs: bool = False

    # Static SPA build directory. Empty disables static serving (API-only, e.g. tests).
    static_dir: str = "/app/static"

    # Worker
    embedded_worker: bool = False  # run the investigation worker inside the API process (dev only)
    worker_concurrency: int = 2
    worker_poll_seconds: float = 1.0

    # Investigation limits (hard ceilings; per-environment settings can only lower them)
    max_tool_calls: int = 16
    max_llm_rounds: int = 4
    max_duration_seconds: int = 180
    max_query_window_minutes: int = 360
    default_query_window_minutes: int = 60
    max_result_bytes: int = 48_000
    max_tokens_per_investigation: int = 60_000
    investigation_lease_seconds: int = 120
    investigation_max_attempts: int = 2  # a crashed run resumes this many times in all
    # Tool calls per intent, under max_tool_calls (0 or absent: max_tool_calls). JSON in MOKS_INTENT_TOOL_BUDGETS.
    intent_tool_budgets: dict[str, int] = {"conceptual": 2, "deployment_guidance": 12, "repository": 10, "execution": 12}
    # Searches the knowledge stage may run per investigation (initial plus one refresh after triage). They are
    # counted in tool_calls but come from this allowance, so documented procedures never cost live evidence.
    knowledge_max_searches: int = 3
    # ML (docs/ML.md). Capability modes live in the ml_capabilities table; these bound the work each one may do.
    ml_disabled: bool = False                 # MOKS_ML_DISABLED: forces every capability off whatever the database says (the deploy-time kill switch)
    ml_learning_capture: bool = True          # write a learning record when an investigation finishes; capturing is not training
    ml_learning_retention_days: int = 365     # records past this are purged by `moks learning purge` and the daily job
    ml_similar_incidents_k: int = Field(default=5, ge=1, le=10)
    ml_similar_incidents_candidates: int = Field(default=300, ge=10, le=2000)
    ml_anomaly_step_seconds: int = Field(default=300, ge=60, le=3600)          # one sample per step, for training and inference alike
    ml_anomaly_window_minutes: int = Field(default=60, ge=15, le=360)          # the window an observation scores
    ml_anomaly_training_days: int = Field(default=14, ge=1, le=60)
    ml_anomaly_min_history_days: float = Field(default=7.0, ge=0.5, le=60)     # less than this is reported as insufficient history
    ml_anomaly_max_cohorts: int = Field(default=40, ge=1, le=500)               # per environment per tick, and per training run
    ml_anomaly_max_series: int = Field(default=200, ge=10, le=5000)             # per query; more is refused rather than truncated
    ml_anomaly_concurrency: int = Field(default=2, ge=1, le=8)
    ml_anomaly_interval_minutes: int = Field(default=15, ge=5, le=240)
    ml_anomaly_max_flags_per_day: float = Field(default=3.0, ge=0.1, le=100)   # promotion gate on the validation period
    ml_anomaly_namespaces: str = ""            # "env:namespace,env:namespace" the scheduled training covers; empty trains nothing
    ml_anomaly_rate_window_seconds: int = Field(default=300, ge=60, le=3600)   # rate() window for every counter feature; never below the step
    ml_anomaly_lookback_hours: int = Field(default=24, ge=1, le=72)            # history read before the window for online detectors (rolling, EWMA, decomposition)
    ml_anomaly_persist_points: int = Field(default=2, ge=1, le=12)             # a feature is flagged only when it stays past its threshold this many points in a row
    ml_anomaly_min_synthetic_recall: float = Field(default=0.5, ge=0, le=1)    # promotion gate: share of faults injected into the validation data that are found
    ml_anomaly_max_detection_delay_minutes: int = Field(default=45, ge=5, le=360)  # promotion gate: median delay on the injected faults
    ml_anomaly_request_metric: str = "http_requests_total"                    # the application's request counter (the mesh's istio_requests_total is the fallback)
    ml_anomaly_duration_metric: str = "http_request_duration_seconds_bucket"
    ml_anomaly_queue_depth_metric: str = ""    # a gauge with namespace and pod labels; empty leaves queue_depth absent
    ml_anomaly_db_connections_metric: str = ""  # a gauge with namespace and pod labels; empty leaves db_connections absent
    ml_correlation_window_minutes: int = Field(default=30, ge=5, le=240)       # anomalies first flagged further apart than this are never grouped
    ml_correlation_lookback_minutes: int = Field(default=120, ge=15, le=1440)  # how far back each tick regroups
    ml_correlation_close_after_minutes: int = Field(default=60, ge=15, le=1440)  # a group with no new anomaly for this long is closed
    ml_correlation_max_namespaces: int = Field(default=5, ge=1, le=40)         # context is read for at most this many namespaces per tick
    ml_artifact_store: Literal["postgres", "s3"] = "postgres"
    ml_artifact_bucket: str = ""
    ml_artifact_prefix: str = "moks/ml-artifacts/"
    ml_artifact_endpoint: str = ""
    ml_artifact_region: str = ""
    ml_artifact_max_bytes: int = 5_000_000
    guidance_max_files: int = 6        # repository files read for one guidance answer
    intent_llm_classifier: bool = True  # ask the chat model only when the rules are unsure
    rag_rerank_enabled: bool = False  # stays off until docs/RERANKING.md's gates pass on real questions
    rag_rerank_url: str = ""          # internal Service only
    rag_rerank_model: str = ""        # must match what the service reports, or results keep their existing order
    rag_rerank_revision: str = ""
    rag_rerank_candidates: int = 30   # rag/rerank.py holds the ceilings, whatever is configured here
    rag_rerank_results: int = 12
    rag_rerank_timeout_ms: int = 1500
    rag_rerank_max_query_chars: int = 1000
    rag_rerank_max_passage_chars: int = 2000
    rag_rerank_min_score: float | None = None  # a reranked passage scoring below this is dropped (exact identifiers never are); empty: none
    rag_rerank_interactive: bool = True  # false: the Knowledge search page skips the reranker; investigations still use it
    rag_decompose: bool = False  # off: the rag dataset measured no gain from it. Turn on only with a dataset that shows one.
    rag_activation: str = "strict"  # strict: a reindex with any incomplete document fails. degraded: it activates and says so.
    rag_query_expansion: bool = False  # SRE synonyms as one more keyword ranking (rag/query.py); the query and its identifiers never change
    rag_require_embedding_evaluation: bool = True  # replacing a model whose index has vectors needs a passed embedding evaluation of the new one
    rag_embedding_eval_tolerance: float = 0.02  # how far below the serving model a candidate may score (nDCG@10, Recall@5) and still pass
    # Fusion weights for reciprocal rank fusion. 1/1/2 is what search did before they were settings: an exact
    # identifier counted twice. 0 leaves a ranker out. rag/service.py bounds each to 0..5.
    rag_weight_keyword: float = 1.0
    rag_weight_vector: float = 1.0
    rag_weight_identifier: float = 2.0
    rag_weight_entity: float = 0.5  # services a question names, resolved against what the asker can read, searched by name
    rag_rrf_k: int = 60
    rag_structured_chunking: bool = True  # alert rules per rule, manifests per resource, ...; false: split by format only
    rag_query_signals: bool = True  # intent, services, environments and time a question names, as small ranking signals
    rag_index_investigations: bool = False  # finished investigations become searchable history, scoped like the investigation
    rag_history_days: int = 180  # how far back investigation history reaches
    # The shared Hybrid RAG service, moks-rag (docs/SHARED_KNOWLEDGE.md). Read without the MOKS_ prefix: these are the names
    # every moks-rag client uses. Off unless the URL and the token are both set; the token comes from a Kubernetes Secret.
    shared_rag_url: str = Field(default="", validation_alias=AliasChoices("RAG_API_URL", "MOKS_SHARED_RAG_URL"))
    shared_rag_token: str = Field(default="", repr=False, validation_alias=AliasChoices("RAG_BEARER_TOKEN", "MOKS_SHARED_RAG_TOKEN"))
    shared_rag_collection: str = Field(default="sre_knowledge", validation_alias=AliasChoices("RAG_COLLECTION", "MOKS_SHARED_RAG_COLLECTION"))
    shared_rag_enabled: bool = True  # Settings > Knowledge search: off sends nothing to moks-rag, even when it is configured
    shared_rag_top_k: int | None = Field(default=None, ge=1, le=50)  # passages per search; empty: rag-api's own default
    evidence_max_age_seconds: int = 900  # older evidence cannot back a claim about the current state without saying so
    # Opt-in CrewAI workload investigation (docs/crewai-investigation.md). Off unless MOKS_CREWAI_ENABLED is true and the
    # image was built with the crewai extra. The global ceilings (max_tool_calls, max_duration_seconds) still apply.
    crewai_enabled: bool = False
    crewai_timeout_seconds: int = Field(default=120, ge=10, le=900)
    crewai_tool_timeout_seconds: int = Field(default=30, ge=1, le=120)
    crewai_max_iterations: int = Field(default=5, ge=1, le=20)  # per agent
    crewai_max_tool_calls: int = Field(default=20, ge=1, le=100)  # across the whole crew
    crewai_max_concurrent: int = Field(default=2, ge=1, le=10)  # crews per worker process
    crewai_slot_wait_seconds: int = Field(default=20, ge=0, le=300)
    crewai_excerpt_chars: int = Field(default=2000, ge=200, le=20000)  # what one tool result shows the model and the report
    crewai_log_lines: int = Field(default=100, ge=10, le=500)
    # The CrewAI worker's run time limit (its MOKS_MAX_DURATION_SECONDS) as the api sees it, for the crews' timeout ceilings. 0: max_duration_seconds.
    crewai_max_duration_seconds: int = Field(default=0, ge=0, le=3600)
    # The incident flow (docs/crewai-sre-engine.md): when the crew cannot start (no slot, flow switched off, wrong crewai version), run the
    # LangGraph investigation instead ("langgraph") or fail ("none"). Console test runs never fall back.
    crewai_fallback: Literal["langgraph", "none"] = "langgraph"
    # Unified investigation pipeline (docs/UNIFIED_INVESTIGATION.md), the CrewAI console flow unified_investigation. Its budgets sit
    # under the global ceilings: tool calls never above max_tool_calls, time never above max_duration_seconds.
    unified_max_tool_calls: int = Field(default=24, ge=4, le=60)
    unified_timeout_seconds: int = Field(default=150, ge=20, le=900)
    unified_agent_timeout_seconds: int = Field(default=60, ge=10, le=600)  # each reasoning agent (analyst, reviewer)
    unified_max_hypotheses: int = Field(default=5, ge=1, le=10)
    unified_max_probes: int = Field(default=4, ge=0, le=12)  # additional evidence calls (stage 11), within the tool budget
    # Metric names for golden signals (prom_golden_signals, the unified investigation). Defaults: Prometheus client conventions and OpenTelemetry's db.client.connections.*.
    unified_request_metric: str = Field(default="http_requests_total", pattern=r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
    unified_duration_metric: str = Field(default="http_request_duration_seconds_bucket", pattern=r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
    unified_status_label: str = Field(default="code", pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    unified_db_connections_metric: str = Field(default="db_client_connections_usage", pattern=r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
    unified_db_connections_max_metric: str = Field(default="db_client_connections_max", pattern=r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")

    # Kubernetes <-> AWS Secrets Manager sync (docs/SECRETS_SYNC.md). Off unless MOKS_SECRETS_SYNC_ENABLED is true in the api and
    # the executor. Values are read and written only by the executor; nothing here schedules a sync, only discovery.
    secrets_sync_enabled: bool = False
    secrets_namespaces: str = ""  # "ns,ns": secretsSync.namespaces, where the executor has a Role; discovery covers these
    secrets_discovery_interval_minutes: int = Field(default=0, ge=0, le=1440)  # executor lists them on its own this often; 0 = only on request
    # manual: every write is a plan someone reviews; schedules are held and never run (discovery still refreshes metadata).
    # scheduled: an approved schedule may write one-sided changes on its own.
    secrets_sync_policy: Literal["manual", "scheduled"] = "manual"
    secrets_digest_key: str = Field(default="", repr=False)  # executor only: keys the comparison digests (32+ characters)
    secrets_max_plan_items: int = Field(default=100, ge=1, le=500)
    secrets_discovery_max_pages: int = Field(default=50, ge=1, le=500)
    secrets_lease_seconds: int = Field(default=120, ge=30, le=900)  # how long one MOKS operation holds a mapping
    secrets_lease_wait_seconds: float = Field(default=10.0, ge=0, le=120)  # how long an item waits for another operation on its mapping

    # Automatic investigations (alert- and detection-triggered) allowed per environment per hour, counted
    # separately for each trigger. Beyond it the incident still opens and notes that the run was skipped.
    auto_investigations_per_hour: int = Field(default=6, ge=1, le=120)

    # Rate limits (per process; see docs/SECURITY.md)
    rate_login_per_minute: int = 10
    rate_investigations_per_minute: int = 10
    rate_api_per_minute: int = 600

    # Retrieval: postgres (full-text, + pgvector when installed) or pinecone (vectors; keyword search stays in PostgreSQL).
    rag_backend: Literal["postgres", "pinecone"] = "postgres"
    pinecone_cloud: str = "aws"
    pinecone_region: str = "us-east-1"
    rag_retired_retention_hours: int = 168  # a replaced generation stays this long, so it can be activated again
    # Database backups: namespaces the executor may dump from (it needs pods/exec there).
    database_backup_namespaces: str = ""
    database_backup_dir: str = "/backup-work"
    database_backup_timeout_seconds: int = 1800
    rag_max_upload_bytes: int = 2_000_000
    rag_chunk_tokens: int = 350
    rag_chunk_overlap_tokens: int = 30

    # Executor and diagnostics
    process_role: Literal["api", "worker", "executor", "terminal"] = "api"
    sync_timezone: str = "UTC"  # shown beside knowledge sync schedules
    database_volume_gib: int = 10  # capacity of the database volume, for the size alert
    diagnostics_namespace: str = "moks-sre"
    velero_namespace: str = "velero"
    # Terminal gateway. Profiles are JSON from the chart, so a change of IAM role is a reviewed change.
    terminal_namespace: str = "moks-sre"
    terminal_image: str = ""
    terminal_image_pull_secrets: str = ""
    terminal_service_account: str = "moks-terminal-session"
    terminal_profiles: str = "[]"
    terminal_kube_namespaces: str = ""
    terminal_idle_minutes: int = 15
    terminal_max_minutes: int = 60
    terminal_user_sessions: int = 2
    terminal_cpu_limit: str = "500m"
    terminal_memory_limit: str = "512Mi"
    argocd_namespace: str = "argocd"
    build_commit: str = "dev"  # baked into the image at build time
    llm_user_daily_budget_usd: float | None = None      # None: no per-person limit
    llm_investigation_budget_usd: float | None = None   # None: no per-investigation limit
    # Which job classes this worker runs, and how many of each at once. Investigations are the
    # "interactive" class and use worker_concurrency.
    worker_classes: str = "detection,interactive,knowledge,maintenance"
    worker_capacity: str = "detection=2,knowledge=1,maintenance=1"
    job_queue_limit: int = 500          # queued jobs per class; beyond this new work is refused
    job_environment_limit: int = 2      # running jobs per environment within a class
    investigation_queue_limit: int = 200
    own_namespace: str = "moks-sre"  # never a restore target: restoring over MOKS would stop the restore

    # Alerts: shared secret for the Alertmanager / Grafana webhook (Authorization: Bearer ...)
    alert_webhook_token: str = Field(default="", repr=False)
    alert_poll_seconds: int = 60

    # Observability
    metrics_port: int = 9464
    log_level: str = "INFO"
    otel_service_name: str = "moks"
    trace_sample_ratio: float = 1.0  # share of new traces kept; children always follow their parent

    @field_validator("base_path")
    @classmethod
    def _normalise_base_path(cls, v: str) -> str:
        v = "/" + v.strip("/")
        return "" if v == "/" else v

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        if self.auth_mode == "dev" and self.env != "development":
            raise ValueError("MOKS_AUTH_MODE=dev is only allowed with MOKS_ENV=development")
        if self.env == "production":
            if len(self.session_secret) < 32:
                raise ValueError("MOKS_SESSION_SECRET must be at least 32 characters in production")
            if self.auth_mode == "oidc" and not (self.oidc_issuer and self.oidc_client_id):
                raise ValueError("MOKS_OIDC_ISSUER and MOKS_OIDC_CLIENT_ID are required for auth_mode=oidc")
        if not self.session_secret:
            # Development/test only: a fixed, obviously insecure value.
            self.session_secret = "dev-insecure-session-secret-change-me-000000"
        return self

    @property
    def api_prefix(self) -> str:
        return f"{self.base_path}/api"

    @property
    def secrets_namespace_list(self) -> list[str]:
        return [n.strip() for n in self.secrets_namespaces.split(",") if n.strip()]

    @property
    def bootstrap_admin_set(self) -> set[str]:
        return {e.strip().lower() for e in self.bootstrap_admins.split(",") if e.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
