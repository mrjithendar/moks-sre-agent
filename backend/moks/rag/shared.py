"""Client for the shared Hybrid RAG service, moks-rag (docs/SHARED_KNOWLEDGE.md).

moks-rag (namespace rag) keeps runbooks, approved postmortems and cluster manifests in Qdrant collections searched by
dense (bge-m3) and sparse (BM25) vectors fused by reciprocal rank. MOKS uses one collection, sre_knowledge by default,
and asks it for passages only (include_answer: false). The answer stays MOKS's: written by its investigation and checked
by its claim validation, never by moks-rag's model.

Off unless RAG_API_URL and RAG_BEARER_TOKEN are both set and Settings > Knowledge search leaves it on. Never a reason for
an investigation to fail:
- one deadline of TIMEOUT_SECONDS covers a call, its connection and its one retry (made only when the connection itself
  failed, so nothing was sent twice);
- every failure is a SharedRagError with a kind, which the tool layer records as missing evidence;
- after FAILURES_TO_OPEN failures in a row the client stops asking for OPEN_SECONDS, so an outage costs a triage one
  wait rather than one per investigation.

Every request carries Authorization: Bearer <RAG_BEARER_TOKEN> and X-Service-Name: moks-sre-agent. The token never
appears in a message, a log line or a result.

Ranking. rag-api fuses its dense and sparse rankings with RRF and takes no weights. In triage the exact strings matter
most (CrashLoopBackOff, exit code 137, ECONNREFUSED, the service's name), so build_query() puts them at the front of the
query, where BM25 matches them literally, and prefer_exact() orders what comes back by how many of them each passage
contains, rag-api's order breaking ties. A passage that names none of them is kept, only ranked after those that do.

Scores. `score` is rag-api's rank-fusion score: it orders one answer and says nothing about similarity. rag-api 0.2.0 also
returns `dense_score`, the passage's cosine similarity to the query, which can be compared across queries; Settings >
Knowledge search > Shared knowledge similarity floor (empty: none) asks rag-api to leave out passages below it. With an
older rag-api `dense_score` is None and the floor is ignored there.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from ..config import get_settings
from ..observability import SHARED_RAG_CALLS, SHARED_RAG_SECONDS
from ..redaction import redact

SERVICE_NAME = "moks-sre-agent"
TIMEOUT_SECONDS = 10.0
FAILURES_TO_OPEN = 3
OPEN_SECONDS = 30.0
ENV_URL, ENV_TOKEN, ENV_COLLECTION = "RAG_API_URL", "RAG_BEARER_TOKEN", "RAG_COLLECTION"
DEFAULT_COLLECTION = "sre_knowledge"
# Bounds on what one query and one result may carry: rag-api embeds the whole query, and results reach a model.
MAX_QUERY_CHARS = 1000
MAX_ERROR_LINES = 6
MAX_LINE_CHARS = 200
MAX_IDENTIFIERS = 16
MAX_PASSAGE_CHARS = 1500
MAX_STEPS = 5
MAX_STEP_CHARS = 220
NOTE = "From the shared SRE knowledge base (moks-rag): documentation and history, never the current state of anything."

# ConnectorError kinds (connectors/base.py) the tool layer reports for each of ours.
CONNECTOR_KIND = {"not_configured": "config", "unavailable": "unavailable", "timeout": "timeout", "auth": "auth", "bad_request": "bad_request",
                  "bad_response": "unavailable", "circuit_open": "unavailable"}


class SharedRagError(Exception):
    """kind: not_configured | unavailable | timeout | auth | bad_request | bad_response | circuit_open"""

    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind, self.message, self.status = kind, message, status

    def __str__(self) -> str:
        return f"{self.kind}: {self.message}"


class SharedRag:
    def __init__(self, url: str, token: str, collection: str = DEFAULT_COLLECTION, *, timeout: float = TIMEOUT_SECONDS,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.url, self.token = (url or "").strip().rstrip("/"), (token or "").strip()
        self.collection = (collection or "").strip() or DEFAULT_COLLECTION
        self.timeout = max(0.1, min(float(timeout), TIMEOUT_SECONDS))
        self._transport = transport
        self._failures = 0
        self._open_until = 0.0
        self._clock = time.monotonic

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    def where(self) -> str:
        """The service's address without credentials or query, for messages."""
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.hostname}{f':{parts.port}' if parts.port else ''}{parts.path}" if parts.hostname else "(not set)"

    def circuit_open(self) -> bool:
        return self._clock() < self._open_until

    # ---- operations ------------------------------------------------------------------------------------

    async def query(self, text: str, *, top_k: int | None = None, filters: dict[str, Any] | None = None,
                    min_dense_score: float | None = None) -> list[dict[str, Any]]:
        """Ranked passages for `text`, best first, each with its rank in rag-api's order and its cosine similarity
        (`dense_score`, None from rag-api < 0.2.0). Never asks moks-rag's model."""
        body: dict[str, Any] = {"collection": self.collection, "query": text[:MAX_QUERY_CHARS], "include_answer": False}
        if top_k:
            body["top_k"] = int(top_k)
        if filters:
            body["filters"] = filters
        if min_dense_score is not None:
            body["min_dense_score"] = float(min_dense_score)
        data = await self._request("query", "POST", "/query", json=body)
        sources = data.get("sources") if isinstance(data, dict) else None
        if not isinstance(sources, list):
            raise self._failed("query", "bad_response", "rag-api answered without a sources list")
        out = []
        for rank, s in enumerate(sources, 1):
            if not isinstance(s, dict) or not isinstance(s.get("text"), str):
                raise self._failed("query", "bad_response", f"rag-api source {rank} has no text")
            meta = s.get("metadata") if isinstance(s.get("metadata"), dict) else {}
            try:
                score = float(s.get("score") or 0.0)
            except (TypeError, ValueError):
                raise self._failed("query", "bad_response", f"rag-api source {rank} has a score that is not a number") from None
            dense = s.get("dense_score")
            dense = float(dense) if isinstance(dense, int | float) and not isinstance(dense, bool) else None
            out.append({"id": str(s.get("id") or ""), "doc_id": str(s.get("doc_id") or meta.get("doc_id") or ""), "score": score, "rank": rank,
                        "dense_score": dense, "text": s["text"], "metadata": meta})
        return out

    async def ingest(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Chunk, embed and upsert one document. rag-api replaces a doc_id's earlier chunks, so this is idempotent."""
        doc_id = str(doc.get("doc_id") or "")
        check_doc_id(doc_id)
        data = await self._request("ingest", "POST", "/ingest", json={**doc, "collection": self.collection})
        if not isinstance(data, dict) or "chunks_ingested" not in data:
            raise self._failed("ingest", "bad_response", "rag-api did not report what it ingested")
        return data

    async def delete(self, doc_id: str) -> dict[str, Any]:
        """Remove every chunk of one document from the collection (a deprecated runbook, a withdrawn postmortem)."""
        check_doc_id(doc_id)
        data = await self._request("delete", "DELETE", f"/documents/{quote(doc_id, safe='')}", params={"collection": self.collection})
        if not isinstance(data, dict) or data.get("deleted") is not True:
            raise self._failed("delete", "bad_response", "rag-api did not confirm the deletion")
        return data

    async def ready(self) -> dict[str, Any]:
        """For status pages: what /ready says, or that it could not be asked. Never raises."""
        if not self.configured:
            return {"configured": False, "reachable": False, "ready": False}
        try:
            async with asyncio.timeout(self.timeout):
                async with self._client() as c:
                    r = await c.get("/ready")
        except (TimeoutError, httpx.HTTPError):
            return {"configured": True, "reachable": False, "ready": False}
        return {"configured": True, "reachable": True, "ready": r.status_code == 200}

    # ---- transport -------------------------------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.url, transport=self._transport, timeout=httpx.Timeout(self.timeout), follow_redirects=False,
                                 headers={"Authorization": f"Bearer {self.token}", "X-Service-Name": SERVICE_NAME})

    async def _send(self, method: str, path: str, json: Any, params: dict | None) -> httpx.Response:
        async with self._client() as c:
            try:
                return await c.request(method, path, json=json, params=params)
            except httpx.ConnectError:  # nothing reached rag-api (a sidecar starting, a pod moving): once more, same deadline
                return await c.request(method, path, json=json, params=params)

    def _failed(self, op: str, kind: str, message: str, status: int | None = None) -> SharedRagError:
        self._failures += 1
        if self._failures >= FAILURES_TO_OPEN:
            self._open_until = self._clock() + OPEN_SECONDS
        SHARED_RAG_CALLS.labels(op, kind).inc()
        return SharedRagError(kind, message, status)

    async def _request(self, op: str, method: str, path: str, *, json: Any = None, params: dict | None = None) -> Any:
        if not self.configured:
            SHARED_RAG_CALLS.labels(op, "not_configured").inc()
            raise SharedRagError("not_configured", f"moks-rag is not configured: {ENV_URL} and {ENV_TOKEN} must both be set")
        if self.circuit_open():
            SHARED_RAG_CALLS.labels(op, "circuit_open").inc()
            raise SharedRagError("circuit_open", f"moks-rag failed {FAILURES_TO_OPEN} times in a row, so it is not asked again for {OPEN_SECONDS:.0f}s")
        start = self._clock()
        try:
            async with asyncio.timeout(self.timeout):
                r = await self._send(method, path, json, params)
        except (TimeoutError, httpx.TimeoutException):
            raise self._failed(op, "timeout", f"moks-rag did not answer within {self.timeout:.0f}s") from None
        except httpx.HTTPError as exc:
            raise self._failed(op, "unavailable", f"moks-rag is unreachable at {self.where()} ({type(exc).__name__})") from None
        finally:
            SHARED_RAG_SECONDS.labels(op).observe(self._clock() - start)
        if r.status_code == 401:
            raise self._failed(op, "auth", f"moks-rag refused the bearer token (HTTP 401); check {ENV_TOKEN}", 401)
        if r.status_code == 403:  # rag-api >= 0.2.0: a per-service token whose access policy leaves out this collection
            raise self._failed(op, "auth", f"moks-rag does not let this token use collection {self.collection!r} (HTTP 403: {_detail(r)}); "
                                           "check the moks-sre-agent entry of rag-api's access policy, or RAG_COLLECTION", 403)
        if r.status_code == 404:
            raise self._failed(op, "bad_request", f"moks-rag has no {path.split('?')[0]} at {self.where()}; check {ENV_URL}", 404)
        if r.status_code in (400, 409, 413, 422):  # our request, not the service: no reason to stop asking
            SHARED_RAG_CALLS.labels(op, "bad_request").inc()
            raise SharedRagError("bad_request", f"moks-rag refused the request (HTTP {r.status_code}): {_detail(r)}", r.status_code)
        if r.status_code != 200:
            raise self._failed(op, "unavailable", f"moks-rag answered HTTP {r.status_code}{_dependency(r)}", r.status_code)
        try:
            body = r.json()
        except ValueError:
            raise self._failed(op, "bad_response", "moks-rag answered with something that is not JSON") from None
        self._failures, self._open_until = 0, 0.0
        SHARED_RAG_CALLS.labels(op, "ok").inc()
        return body


def _dependency(r: httpx.Response) -> str:
    """rag-api >= 0.2.0 names the service behind it that failed (bge-m3, qdrant, docling-serve, litellm)."""
    try:
        body = r.json()
    except ValueError:
        return ""
    dep = body.get("dependency") if isinstance(body, dict) else None
    kind = body.get("kind") if isinstance(body, dict) else None
    if not isinstance(dep, str) or not re.fullmatch(r"[a-z0-9-]{1,40}", dep):
        return ""
    return f" ({dep} {kind})" if isinstance(kind, str) and re.fullmatch(r"[a-z_]{1,20}", kind) else f" ({dep})"


def _detail(r: httpx.Response) -> str:
    try:
        d = r.json().get("detail")
    except (ValueError, AttributeError):
        d = r.text
    return redact(str(d))[:300]


def check_doc_id(doc_id: str) -> None:
    """rag-api routes DELETE /documents/{doc_id} on the decoded path, so a doc_id with a slash could be ingested but never
    deleted. Ids made here never have one (doc_id_for); this refuses one made elsewhere before it is sent."""
    if not doc_id or "/" in doc_id or len(doc_id) > 240:
        raise SharedRagError("bad_request", f"doc_id {doc_id[:80]!r} must be 1-240 characters without '/'")


def doc_id_for(*parts: str) -> str:
    """A stable, readable id with no slash: ("moks-sre-agent", "docs/OPERATOR_GUIDE.md") -> moks-sre-agent:docs:OPERATOR_GUIDE.md."""
    raw = ":".join(p.strip("/").replace("/", ":") for p in parts if p)
    clean = re.sub(r"[^A-Za-z0-9._:@+-]", "-", raw)
    return clean if len(clean) <= 200 else f"{clean[:180]}~{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


# ---- the configured client ---------------------------------------------------------------------------------

TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests put an httpx.MockTransport here
_client: SharedRag | None = None
_key: tuple | None = None


def configured(s: Any = None) -> bool:
    s = s or get_settings()
    return bool(s.shared_rag_url and s.shared_rag_token)


def enabled(s: Any = None) -> bool:
    s = s or get_settings()
    return configured(s) and bool(s.shared_rag_enabled)


def why_off(s: Any = None) -> str:
    s = s or get_settings()
    if not s.shared_rag_url and not s.shared_rag_token:
        return f"the shared SRE knowledge base (moks-rag) is not configured: {ENV_URL} and {ENV_TOKEN} are not set"
    if not s.shared_rag_url or not s.shared_rag_token:
        return f"the shared SRE knowledge base (moks-rag) is not configured: {ENV_TOKEN if s.shared_rag_url else ENV_URL} is not set"
    if not s.shared_rag_enabled:
        return "the shared SRE knowledge base (moks-rag) is switched off in Settings > Knowledge search"
    return ""


def active() -> SharedRag | None:
    """The client for this process, or None while it is not configured or switched off. One client per configuration, so
    its circuit breaker sees every investigation this process runs."""
    global _client, _key
    s = get_settings()
    if not enabled(s):
        return None
    key = (s.shared_rag_url, hashlib.sha256(s.shared_rag_token.encode()).hexdigest(), s.shared_rag_collection, id(TRANSPORT))
    if key != _key or _client is None:
        _key, _client = key, SharedRag(s.shared_rag_url, s.shared_rag_token, s.shared_rag_collection, transport=TRANSPORT)
    return _client


def from_env(environ: Mapping[str, str] | None = None, transport: httpx.AsyncBaseTransport | None = None) -> SharedRag:
    """For the CLI and CI: the same three variables, without the rest of MOKS's settings."""
    env = os.environ if environ is None else environ
    return SharedRag(env.get(ENV_URL, ""), env.get(ENV_TOKEN, ""), env.get(ENV_COLLECTION) or DEFAULT_COLLECTION, transport=transport)


def status() -> dict[str, Any]:
    s = get_settings()
    c = active()
    probe = c or SharedRag(s.shared_rag_url, "", s.shared_rag_collection)
    return {"configured": configured(s), "enabled": enabled(s), "url": probe.where() if s.shared_rag_url else None, "collection": probe.collection,
            "circuit_open": bool(c and c.circuit_open()), "reason": why_off(s) or None}


# ---- the triage query --------------------------------------------------------------------------------------

K8S_REASONS = ("CrashLoopBackOff", "OOMKilled", "ImagePullBackOff", "ErrImagePull", "ErrImageNeverPull", "InvalidImageName", "CreateContainerConfigError",
               "CreateContainerError", "RunContainerError", "ContainerCannotRun", "ContainerStatusUnknown", "StartError", "Evicted", "Preempting",
               "FailedScheduling", "FailedMount", "FailedAttachVolume", "FailedCreatePodSandBox", "FailedKillPod", "FailedCreate", "FailedSync",
               "BackOff", "Unhealthy", "ProbeWarning", "NodeNotReady", "NodeHasDiskPressure", "NodeHasInsufficientMemory", "DiskPressure", "MemoryPressure",
               "PIDPressure", "NetworkNotReady", "DeadlineExceeded", "BackoffLimitExceeded", "ProgressDeadlineExceeded", "ReplicaFailure", "FailedGetScale",
               "FailedComputeMetricsReplicas", "ComparisonError", "OutOfSync", "SyncFailed", "Degraded", "NotTriggerScaleUp")
_REASONS = {r.lower(): r for r in K8S_REASONS}
_REASON_RE = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(K8S_REASONS) + r")(?![A-Za-z0-9])", re.I)
_EXIT_RE = re.compile(r"(?i)\bexit(?:ed)?[\s_-]*(?:with[\s_-]*)?(?:code|status)?[\s:=\"']*(\d{1,3})(?!\d)")
_SIGNAL_RE = re.compile(r"\b(SIG(?:KILL|TERM|SEGV|ABRT|BUS|PIPE|HUP|INT|QUIT))\b")
_ERRNO_RE = re.compile(r"\b(ECONNREFUSED|ECONNRESET|ECONNABORTED|ETIMEDOUT|EHOSTUNREACH|ENETUNREACH|ENOTFOUND|EAI_AGAIN|EADDRINUSE|EACCES|EPERM|ENOSPC|EMFILE|EPIPE|ENOMEM)\b")
_GRPC_RE = re.compile(r"\b(DEADLINE_EXCEEDED|RESOURCE_EXHAUSTED|UNAVAILABLE|PERMISSION_DENIED|UNAUTHENTICATED|FAILED_PRECONDITION)\b")
_CLASS_RE = re.compile(r"\b(?:[a-z_][a-z0-9_]*\.)*([A-Z][A-Za-z0-9]+(?:Error|Exception))\b")
_HTTP_RE = re.compile(r"(?i)\b(?:http(?:/\d(?:\.\d)?)?|status(?:[\s_-]*code)?|response[\s_-]*code)[\s:=\"']*([45]\d\d)(?!\d)")
PHRASES = ("no healthy upstream", "upstream connect error", "connection refused", "connection reset by peer", "context deadline exceeded",
           "i/o timeout", "too many open files", "no space left on device", "x509: certificate", "certificate has expired", "too many connections",
           "too many clients", "deadlock detected", "out of memory", "readiness probe failed", "liveness probe failed", "back-off restarting failed container")
_PHRASE_RE = re.compile("|".join(re.escape(p) for p in PHRASES), re.I)

ERRORISH = re.compile(r"(?i)\b(error|err|fatal|panic|exception|traceback|fail(?:ed|ure|ing)?|refused|time[ds]?[\s-]?out|denied|unavailable|oom\w*|killed|"
                      r"crash\w*|cannot|could not|unable to|exceeded|unhealthy|back-?off|evicted|not ready|unreachable)\b")
_NOISE = [
    (re.compile(r"^\s*\[?\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:?\d\d)?\]?\s*"), ""),   # leading ISO timestamp
    (re.compile(r"^\s*[A-Z][a-z]{2}\s+\d{1,2}\s\d\d:\d\d:\d\d\s+"), ""),                               # leading syslog timestamp
    (re.compile(r"^[IWEF]\d{4}\s+\d\d:\d\d:\d\d\.\d+\s+\d+\s+\S+:\d+\]\s*"), ""),                      # klog header
    (re.compile(r"\b(?:ts|time|timestamp)=\S+\s*"), ""),                                               # logfmt time
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<id>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<id>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
]


@dataclass
class Triage:
    """What an alert or an incident says, in fields MOKS resolved itself. errors: alert descriptions and log lines."""
    alertname: str | None = None
    namespace: str | None = None
    workload: str | None = None
    pod: str | None = None
    container: str | None = None
    service: str | None = None
    errors: list[str] = field(default_factory=list)
    text: str = ""


@dataclass
class Query:
    text: str
    identifiers: list[str]  # exact strings a passage is preferred for: service names first, then codes and reasons
    names: list[str]
    errors: list[str]       # the normalised error lines that went into the query
    label: str


_POD_SUFFIX = re.compile(r"^(?P<base>[a-z0-9][a-z0-9.-]*?)-(?:[bcdfghjklmnpqrstvwxz2-9]{6,10}-[bcdfghjklmnpqrstvwxz2-9]{5}|[bcdfghjklmnpqrstvwxz2-9]{5}|\d+)$")


def workload_of(pod: str | None) -> str | None:
    """payments-7c9d8f6b5c-x2kq9 -> payments, postgres-0 -> postgres, node-exporter-x2kq9 -> node-exporter. Kubernetes
    naming, used to name the query and rank passages, never to decide what a pod belongs to."""
    if not pod:
        return None
    m = _POD_SUFFIX.match(pod)
    if m:
        return m.group("base")
    parts = pod.split("-")
    return "-".join(parts[:-2]) if len(parts) >= 3 else None


def normalise(line: str) -> str:
    out = " ".join(str(line).split())
    for rx, repl in _NOISE:
        out = rx.sub(repl, out)
    return out.strip(" |-")


def _canon(kind: str, value: str) -> str:
    if kind == "reason":
        return _REASONS.get(value.lower(), value)
    if kind == "exit":
        return f"exit code {int(value)}"
    if kind == "http":
        return f"HTTP {value}"
    if kind == "phrase":
        return value.lower()
    return value


def identifiers(texts: Iterable[str], names: Iterable[str] = ()) -> list[str]:
    """Service names, then the exact codes found in `texts`, each once, in the order they first appear."""
    out: list[str] = []
    seen: set[str] = set()

    def add(v: str) -> None:
        if v and v.lower() not in seen and len(out) < MAX_IDENTIFIERS:
            seen.add(v.lower())
            out.append(v)

    for n in names:
        add(n)
    for t in texts:
        t = str(t or "")
        found: list[tuple[int, str]] = []
        for kind, rx in (("reason", _REASON_RE), ("exit", _EXIT_RE), ("signal", _SIGNAL_RE), ("errno", _ERRNO_RE), ("grpc", _GRPC_RE), ("class", _CLASS_RE),
                         ("http", _HTTP_RE), ("phrase", _PHRASE_RE)):
            for m in rx.finditer(t):
                value = m.group(1) if m.groups() else m.group(0)
                if kind == "exit" and int(value) == 0:
                    continue
                found.append((m.start(), _canon(kind, value)))
        for _, v in sorted(found):
            add(v)
    return out


def error_lines(texts: Iterable[str], limit: int = MAX_ERROR_LINES) -> list[str]:
    """The lines that look like errors, normalised (timestamps, ids and addresses taken out), each once: a line already
    contained in one kept earlier (a finding's title after the finding) is not repeated."""
    seen: list[str] = []
    out: list[str] = []
    for t in texts:
        for line in str(t or "").splitlines():
            ln = normalise(line)
            if len(ln) < 12 or not (ERRORISH.search(ln) or _REASON_RE.search(ln) or _EXIT_RE.search(ln) or _PHRASE_RE.search(ln)):
                continue
            key = re.sub(r"\d+", "#", ln.lower())
            if any(key in k for k in seen):
                continue
            seen.append(key)
            out.append(redact(ln)[:MAX_LINE_CHARS])
            if len(out) >= limit:
                return out
    return out


def build_query(t: Triage) -> Query:
    """The hybrid query: alert name, workload and namespace first, then the exact codes, the summary and the error lines."""
    workload = t.workload or workload_of(t.pod)
    names = list(dict.fromkeys(n for n in (workload, t.service) if n))
    errors = error_lines(t.errors)
    ids = identifiers([t.text, *t.errors], names)
    head = " ".join(x for x in (t.alertname, workload, f"namespace {t.namespace}" if t.namespace and t.namespace != "*" else None,
                                f"pod {t.pod}" if t.pod and t.pod != workload else None, f"container {t.container}" if t.container else None) if x)
    codes = " ".join(i for i in ids if i not in names)
    summary = " ".join(str(t.text or "").split())[:300]
    text = redact(" | ".join(p for p in (head, codes, summary, *errors) if p))[:MAX_QUERY_CHARS]
    label = " ".join(x for x in (t.alertname, f"{t.namespace}/{workload}" if t.namespace and workload else workload or t.namespace) if x) or summary[:60]
    return Query(text=text, identifiers=ids, names=names, errors=errors, label=label)


def _pattern(ident: str) -> re.Pattern[str]:
    m = re.fullmatch(r"exit code (\d+)", ident)
    if m:
        return re.compile(rf"(?i)\b(?:exit|code|status)\D{{0,12}}{m.group(1)}(?!\d)")
    m = re.fullmatch(r"HTTP (\d{3})", ident)
    if m:
        return re.compile(rf"(?<![\d.]){m.group(1)}(?![\d.])")
    words = [re.escape(w) for w in re.split(r"[\s_]+", ident) if w]
    return re.compile(r"(?i)(?<![A-Za-z0-9])" + r"[\s_:=-]*".join(words) + r"(?![A-Za-z0-9-])")


def prefer_exact(passages: list[dict[str, Any]], idents: list[str]) -> list[dict[str, Any]]:
    """Passages naming more of the exact identifiers first; rag-api's rank breaks ties. Each passage keeps what it matched."""
    pats = [(i, _pattern(i)) for i in idents]
    for p in passages:
        hay = f"{(p.get('metadata') or {}).get('title') or ''}\n{p.get('text') or ''}"
        p["matched"] = [i for i, rx in pats if rx.search(hay)]
    return sorted(passages, key=lambda p: (-len(p["matched"]), p.get("rank", 0)))


def in_scope(passages: list[dict[str, Any]], environment: str, allows: Callable[[str], bool]) -> tuple[list[dict[str, Any]], int]:
    """The shared collection serves every MOKS environment with one token, so MOKS applies its own scope: a passage tied to
    another environment, or to a namespace this person may not read, is left out. Untied passages are general guidance."""
    kept = []
    for p in passages:
        m = p.get("metadata") or {}
        env, ns = m.get("environment"), m.get("namespace")
        if env not in (None, "", "*") and str(env) != environment:
            continue
        if ns not in (None, "", "*") and not allows(str(ns)):
            continue
        kept.append(p)
    return kept, len(passages) - len(kept)


# ---- what a passage is ---------------------------------------------------------------------------------------

KINDS = ("runbook", "postmortem", "architecture", "manifest", "document")
_KIND_ALIASES = {"post-mortem": "postmortem", "postmortems": "postmortem", "incident": "postmortem", "rca": "postmortem", "playbook": "runbook",
                 "runbooks": "runbook", "k8s_manifest": "manifest", "helm_values": "manifest", "helm_template": "manifest", "kustomize": "manifest",
                 "istio": "manifest", "architecture_doc": "architecture", "design": "architecture", "documentation": "document", "readme": "document"}
META_KEPT = ("doc_type", "service", "severity", "incident_id", "date", "environment", "namespace", "path", "repository", "revision", "status", "chunk_index",
             "chunk_count", "ingested_by", "heading_path")


def kind_of(p: dict[str, Any]) -> str:
    m = p.get("metadata") or {}
    declared = str(m.get("doc_type") or m.get("kind") or "").strip().lower()
    declared = _KIND_ALIASES.get(declared, declared)
    if declared in KINDS:
        return declared
    if m.get("incident_id"):
        return "postmortem"
    name = " ".join(str(m.get(k) or "") for k in ("title", "source", "filename", "path")).lower()
    if "postmortem" in name or "post-mortem" in name:
        return "postmortem"
    if re.search(r"\.(ya?ml|json|tf)\b", name):
        return "manifest"
    if "runbook" in name or "playbook" in name:
        return "runbook"
    return "document"


def link_of(p: dict[str, Any]) -> str | None:
    m = p.get("metadata") or {}
    for k in ("url", "link", "source"):
        v = str(m.get(k) or "")
        if re.match(r"^https?://", v, re.I):
            return v
    return None


_STEP = re.compile(r"^\s*(?:\d{1,2}[.)]|[-*+]|\[[ xX]\])\s+(.{6,})$")
_CMD = re.compile(r"^\s*(?:\$\s+)?((?:kubectl|helm|istioctl|argocd|flux|curl|psql|redis-cli|velero|k9s)\s.+)$")


def steps_of(text: str, limit: int = MAX_STEPS) -> list[str]:
    """The procedure in a runbook passage: its numbered or bulleted items and its commands, in order."""
    out: list[str] = []
    in_code = False
    for line in str(text or "").splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        cmd = _CMD.match(line)
        step = None if in_code else _STEP.match(line)
        if cmd and (in_code or not step):
            s = f"`{' '.join(cmd.group(1).split())[:MAX_STEP_CHARS - 2]}`"
        elif step:
            s = " ".join(step.group(1).split())[:MAX_STEP_CHARS]
        else:
            continue
        if s not in out:
            out.append(redact(s))
        if len(out) >= limit:
            break
    return out


def view(p: dict[str, Any]) -> dict[str, Any]:
    """A passage as MOKS stores and shows it: text bounded, metadata reduced to what describes the document."""
    m = p.get("metadata") or {}
    return {"id": p.get("id"), "doc_id": p.get("doc_id"), "rank": p.get("rank"), "score": p.get("score"), "dense_score": p.get("dense_score"), "kind": kind_of(p),
            "title": str(m.get("title") or m.get("filename") or p.get("doc_id") or "")[:300], "url": link_of(p), "matched": list(p.get("matched") or []),
            "steps": steps_of(p.get("text") or "") if kind_of(p) in ("runbook", "document", "architecture") else [],
            "text": redact(str(p.get("text") or ""))[:MAX_PASSAGE_CHARS], "metadata": {k: m[k] for k in META_KEPT if k in m}}
