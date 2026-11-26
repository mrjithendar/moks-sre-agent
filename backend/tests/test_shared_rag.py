"""MOKS with the shared moks-rag service (docs/SHARED_KNOWLEDGE.md): the client, the triage query and its exact-identifier
ranking, ingestion and deletion, approved postmortems, alert triage grounded in shared runbooks, and triage that carries on
when moks-rag is down. rag-api is an httpx.MockTransport speaking its API (moks-rag app/src/rag_api/main.py)."""
import argparse
import asyncio
import json
import time
import uuid
from collections import Counter
from types import SimpleNamespace
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from moks.config import get_settings
from moks.connectors import registry
from moks.db import session_scope
from moks.models import Environment, Evidence, IncidentEvent, Investigation, Job
from moks.orchestrator.engine import Investigator
from moks.rag import shared, shared_ingest
from tests.conftest import login, make_env, make_user
from tests.fakes import FakeKubernetes

TOKEN = "rag-test-token-0123456789abcdef"
URL = "http://rag-api.rag.svc.cluster.local:8000"

GENERIC = {"id": "p1", "doc_id": "moks-sre-agent:docs:OPERATOR_GUIDE.md", "score": 0.033, "text": "General guidance on restarting MOKS components safely.",
           "metadata": {"doc_type": "runbook", "title": "Operator guide"}}
RUNBOOK = {"id": "p2", "doc_id": "moks-sre-agent:docs:runbooks:payments.md", "score": 0.031,
           "text": "# Payments crash loop\n\nWhen payments pods are in CrashLoopBackOff:\n1. Read the previous container's logs\n2. Check the database connection pool\n"
                   "```\nkubectl -n shop rollout undo deploy/payments\n```",
           "metadata": {"doc_type": "runbook", "title": "Payments crash loop", "url": "https://github.com/mrjithendar/moks-sre-agent/blob/master/docs/runbooks/payments.md"}}
POSTMORTEM = {"id": "p3", "doc_id": "moks-postmortem:inc-41", "score": 0.02, "text": "Payments crash looped after the database password rotated; db timeout in every log.",
              "metadata": {"doc_type": "postmortem", "title": "Postmortem: payments db timeout", "incident_id": "inc-41", "service": "payments", "severity": "sev2",
                           "date": "2026-10-02", "url": "https://jithendar.com/moks/incidents/inc-41"}}
ELSEWHERE = {"id": "p4", "doc_id": "moks-postmortem:inc-99", "score": 0.019, "text": "payments CrashLoopBackOff in another environment",
             "metadata": {"doc_type": "postmortem", "title": "Postmortem: elsewhere", "environment": "some-other-env", "incident_id": "inc-99"}}


class Rag:
    """rag-api: records every request; `fail` makes it unreachable ("connect"), slow ("timeout") or answer an HTTP status."""

    def __init__(self, sources=None):
        self.calls: list[dict] = []
        self.sources = [GENERIC, RUNBOOK, POSTMORTEM, ELSEWHERE] if sources is None else sources
        self.fail: str | int | None = None

    def handler(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        self.calls.append({"method": req.method, "path": req.url.path, "params": dict(req.url.params), "headers": dict(req.headers), "body": body})
        if self.fail == "connect":
            raise httpx.ConnectError("connection refused", request=req)
        if self.fail == "timeout":
            raise httpx.ReadTimeout("no answer", request=req)
        if isinstance(self.fail, int):
            return httpx.Response(self.fail, json={"detail": "nope"})
        if req.url.path == "/query":
            return httpx.Response(200, json={"query": body["query"], "collection": body["collection"], "model": "claude-sonnet-5-5", "answer": None,
                                             "sources": self.sources, "service": req.headers.get("x-service-name")})
        if req.url.path == "/ingest":
            return httpx.Response(200, json={"doc_id": body["doc_id"], "collection": body["collection"], "chunks_ingested": 2, "parsed_via_docling": False, "service": "x"})
        if req.method == "DELETE" and req.url.path.startswith("/documents/"):
            return httpx.Response(200, json={"doc_id": req.url.path.split("/", 2)[2], "collections": [req.url.params["collection"]], "deleted": True, "service": "x"})
        if req.url.path == "/ready":
            return httpx.Response(200, json={"status": "ready"})
        return httpx.Response(404, json={"detail": "Not Found"})

    @property
    def queries(self) -> list[str]:
        return [c["body"]["query"] for c in self.calls if c["path"] == "/query"]


def client_for(r: Rag, **kw) -> shared.SharedRag:
    return shared.SharedRag(URL, TOKEN, "sre_knowledge", transport=httpx.MockTransport(r.handler), **kw)


@pytest.fixture
def rag(monkeypatch):
    """moks-rag configured for this process, as RAG_API_URL / RAG_BEARER_TOKEN / RAG_COLLECTION would set it."""
    r = Rag()
    s = get_settings()
    for k, v in (("shared_rag_url", URL), ("shared_rag_token", TOKEN), ("shared_rag_collection", "sre_knowledge"), ("shared_rag_enabled", True), ("shared_rag_top_k", None)):
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(shared, "TRANSPORT", httpx.MockTransport(r.handler))
    monkeypatch.setattr(shared, "_client", None)
    monkeypatch.setattr(shared, "_key", None)
    return r


# ---- configuration ---------------------------------------------------------------------------------------------


def test_reads_the_three_variables_without_the_moks_prefix(monkeypatch):
    from moks.config import Settings

    monkeypatch.setenv("RAG_API_URL", URL)
    monkeypatch.setenv("RAG_BEARER_TOKEN", TOKEN)
    monkeypatch.setenv("RAG_COLLECTION", "sre_knowledge")
    s = Settings()
    assert (s.shared_rag_url, s.shared_rag_token, s.shared_rag_collection) == (URL, TOKEN, "sre_knowledge")
    assert TOKEN not in repr(s)
    assert shared.from_env({"RAG_API_URL": URL, "RAG_BEARER_TOKEN": TOKEN}).collection == "sre_knowledge"


def test_the_settings_switch_and_passage_count_are_runtime_settings():
    from moks import runtime_settings as rs

    assert rs.SPECS["shared_rag_enabled"].group == "knowledge" and rs.SPECS["shared_rag_enabled"].kind == "bool"
    assert rs.check(rs.SPECS["shared_rag_top_k"], "") is None  # empty: rag-api's own default, never a number picked here
    assert rs.check(rs.SPECS["shared_rag_top_k"], 8) == 8
    with pytest.raises(rs.Invalid):
        rs.check(rs.SPECS["shared_rag_top_k"], 51)


def test_the_tool_is_offered_only_while_configured_and_switched_on(rag, monkeypatch):
    from moks.tools.contracts import has_connector

    assert has_connector("shared_knowledge", {})
    monkeypatch.setattr(get_settings(), "shared_rag_enabled", False)
    assert not has_connector("shared_knowledge", {}) and shared.active() is None
    assert "switched off" in shared.why_off()
    monkeypatch.setattr(get_settings(), "shared_rag_enabled", True)
    monkeypatch.setattr(get_settings(), "shared_rag_token", "")
    assert not has_connector("shared_knowledge", {}) and "RAG_BEARER_TOKEN is not set" in shared.why_off()


def test_the_tool_is_documentation_with_a_unique_name_and_the_crew_tool_is_bound_to_the_target():
    from moks.crew import incident_config as ic
    from moks.crew.tools import BY_NAME, Refused, Target, bind
    from moks.orchestrator.evidence import DOCUMENTATION, classify
    from moks.tools.builtin import REGISTRY, _all_tools

    assert max(Counter(t.name for t in _all_tools()).values()) == 1  # a duplicate would silently shadow a tool in REGISTRY
    assert REGISTRY["shared_kb_search"].read_only and not REGISTRY["shared_kb_search"].privileged
    assert classify(ok=True, tool="shared_kb_search") == DOCUMENTATION  # documentation can never back an observed fact
    t = Target("prod", "shop", "payments", "Deployment", 60, "")
    assert bind(BY_NAME["shared_runbooks"], t, {"query": "crash loop", "errors": ["exit code 137"]}, log_lines=100) == {
        "namespace": "shop", "workload": "payments", "errors": ["exit code 137"], "text": "crash loop"}
    with pytest.raises(Refused):
        bind(BY_NAME["shared_runbooks"], t, {"namespace": "kube-system"}, log_lines=100)
    assert "shared_runbooks" in ic.TOOLS_BY_AGENT["knowledge"] and "shared_runbooks" in ic.REQUESTABLE


# ---- the client -----------------------------------------------------------------------------------------------


async def test_query_carries_the_token_and_service_name_and_asks_for_passages_only():
    r = Rag()
    hits = await client_for(r).query("KubePodCrashLooping payments", top_k=8)
    call = r.calls[0]
    assert (call["method"], call["path"]) == ("POST", "/query")
    assert call["headers"]["authorization"] == f"Bearer {TOKEN}" and call["headers"]["x-service-name"] == "moks-sre-agent"
    assert call["body"] == {"collection": "sre_knowledge", "query": "KubePodCrashLooping payments", "include_answer": False, "top_k": 8}
    assert [h["rank"] for h in hits] == [1, 2, 3, 4] and hits[1]["doc_id"] == RUNBOOK["doc_id"] and hits[1]["metadata"]["title"] == "Payments crash loop"


@pytest.mark.parametrize("fail,kind", [("connect", "unavailable"), ("timeout", "timeout"), (401, "auth"), (403, "auth"), (503, "unavailable"), (500, "unavailable"),
                                       (422, "bad_request"), (404, "bad_request")])
async def test_every_failure_has_a_kind_and_never_shows_the_token(fail, kind):
    r = Rag()
    r.fail = fail
    with pytest.raises(shared.SharedRagError) as err:
        await client_for(r).query("q")
    assert err.value.kind == kind and TOKEN not in str(err.value) and TOKEN not in err.value.message
    if fail == "connect":
        assert len(r.calls) == 2  # nothing reached rag-api, so once more within the same deadline
    elif fail == "timeout":
        assert len(r.calls) == 1  # a request that may have arrived is never sent twice


async def test_answers_that_are_not_rag_api_answers_are_refused():
    for body in ("not json", {"sources": "nope"}, {"sources": [{"doc_id": "x"}]}, {"sources": [{"text": "t", "score": "high"}]}):
        c = shared.SharedRag(URL, TOKEN, transport=httpx.MockTransport(lambda req, b=body: httpx.Response(200, content=b.encode()) if isinstance(b, str) else httpx.Response(200, json=b)))
        with pytest.raises(shared.SharedRagError) as err:
            await c.query("q")
        assert err.value.kind == "bad_response"


async def test_one_deadline_bounds_a_hanging_service():
    async def slow(req):
        await asyncio.sleep(5)
        return httpx.Response(200, json={"sources": []})

    c = shared.SharedRag(URL, TOKEN, transport=httpx.MockTransport(slow), timeout=0.2)
    started = time.monotonic()
    with pytest.raises(shared.SharedRagError) as err:
        await c.query("q")
    assert err.value.kind == "timeout" and time.monotonic() - started < 2
    assert shared.SharedRag(URL, TOKEN, timeout=60).timeout == shared.TIMEOUT_SECONDS  # never more than 10 s, whatever is asked


async def test_the_circuit_opens_after_repeated_failures_and_closes_after_a_success():
    r = Rag()
    r.fail = 503
    c = client_for(r)
    now = [1000.0]
    c._clock = lambda: now[0]
    for _ in range(shared.FAILURES_TO_OPEN):
        with pytest.raises(shared.SharedRagError):
            await c.query("q")
    sent = len(r.calls)
    with pytest.raises(shared.SharedRagError) as err:
        await c.query("q")
    assert err.value.kind == "circuit_open" and len(r.calls) == sent  # an outage costs one wait, not one per investigation
    now[0] += shared.OPEN_SECONDS + 1
    r.fail = None
    assert await c.query("q") and not c.circuit_open()
    r.fail = 422  # a refused request is ours, not an outage: it never opens the circuit
    for _ in range(shared.FAILURES_TO_OPEN + 1):
        with pytest.raises(shared.SharedRagError) as err:
            await c.query("q")
        assert err.value.kind == "bad_request"


async def test_not_configured_sends_nothing():
    r = Rag()
    c = shared.SharedRag("", "", transport=httpx.MockTransport(r.handler))
    with pytest.raises(shared.SharedRagError) as err:
        await c.query("q")
    assert err.value.kind == "not_configured" and r.calls == []
    assert await c.ready() == {"configured": False, "reachable": False, "ready": False}


async def test_ingest_and_delete_target_the_collection_and_refuse_a_doc_id_with_a_slash():
    r = Rag()
    c = client_for(r)
    assert (await c.ingest({"doc_id": "moks-sre-agent:docs:a.md", "text": "x", "title": "A"}))["chunks_ingested"] == 2
    assert r.calls[-1]["body"]["collection"] == "sre_knowledge"
    await c.delete("moks-sre-agent:docs:a.md")
    assert (r.calls[-1]["method"], r.calls[-1]["path"], r.calls[-1]["params"]) == ("DELETE", "/documents/moks-sre-agent:docs:a.md", {"collection": "sre_knowledge"})
    sent = len(r.calls)
    with pytest.raises(shared.SharedRagError) as err:
        await c.delete("docs/a.md")  # rag-api could never route its DELETE: refused before anything is sent
    assert err.value.kind == "bad_request" and len(r.calls) == sent
    assert "/" not in shared.doc_id_for("moks-sre-agent", "docs/runbooks/payments.md")


# ---- the triage query ------------------------------------------------------------------------------------------


def test_the_query_names_the_alert_namespace_workload_and_the_exact_codes():
    q = shared.build_query(shared.Triage(
        alertname="KubePodCrashLooping", namespace="shop", pod="payments-7c9d8f6b5c-x2kq9", container="app", text="Pod shop/payments is crash looping",
        errors=['2026-11-26T10:00:00Z level=error msg="dial tcp 10.1.2.3:5432: connect: connection refused" err=ECONNREFUSED',
                "Last State: Terminated  Reason: OOMKilled  Exit Code: 137", "reason: CrashLoopBackOff", "INFO request served in 12ms"]))
    assert q.text.startswith("KubePodCrashLooping payments namespace shop pod payments-7c9d8f6b5c-x2kq9 container app")
    assert q.identifiers[0] == "payments"
    assert {"OOMKilled", "exit code 137", "ECONNREFUSED", "connection refused", "CrashLoopBackOff"} <= set(q.identifiers)
    assert any("connection refused" in e and "<ip>" in e and "2026-11-26" not in e for e in q.errors)  # timestamps and addresses taken out
    assert not any("request served" in e for e in q.errors)
    assert len(q.text) <= shared.MAX_QUERY_CHARS


@pytest.mark.parametrize("pod,workload", [("payments-7c9d8f6b5c-x2kq9", "payments"), ("postgres-0", "postgres"), ("node-exporter-x2kq9", "node-exporter"),
                                          ("payments-5c4-xyz", "payments"), ("solo", None)])
def test_workload_from_pod_names(pod, workload):
    assert shared.workload_of(pod) == workload


def test_passages_naming_the_exact_codes_rank_first_and_none_is_dropped():
    hits = [{**GENERIC, "rank": 1}, {**POSTMORTEM, "rank": 2}, {**RUNBOOK, "rank": 3}]
    ranked = shared.prefer_exact(hits, ["payments", "CrashLoopBackOff", "exit code 137"])
    assert [p["id"] for p in ranked] == ["p2", "p3", "p1"]
    assert ranked[0]["matched"] == ["payments", "CrashLoopBackOff"] and ranked[-1]["matched"] == []


def test_another_environment_and_a_namespace_outside_scope_are_left_out():
    hits = [{**RUNBOOK, "rank": 1}, {**ELSEWHERE, "rank": 2}, {"id": "p5", "doc_id": "d", "text": "t", "rank": 3, "metadata": {"namespace": "secure"}},
            {"id": "p6", "doc_id": "e", "text": "t", "rank": 4, "metadata": {"namespace": "shop", "environment": "prod"}}]
    kept, dropped = shared.in_scope(hits, "prod", lambda ns: ns == "shop")
    assert [p["id"] for p in kept] == ["p2", "p6"] and dropped == 2


def test_runbook_steps_kinds_and_links():
    v = shared.view({**RUNBOOK, "rank": 1, "matched": ["payments"]})
    assert v["kind"] == "runbook" and v["steps"] == ["Read the previous container's logs", "Check the database connection pool", "`kubectl -n shop rollout undo deploy/payments`"]
    assert shared.kind_of(POSTMORTEM) == "postmortem" and shared.kind_of({"metadata": {"incident_id": "x"}}) == "postmortem"
    assert shared.kind_of({"metadata": {"filename": "values.yaml"}}) == "manifest"
    assert shared.link_of({"metadata": {"url": "javascript:alert(1)"}}) is None


# ---- ingestion -------------------------------------------------------------------------------------------------


def _repo(tmp_path):
    (tmp_path / "deploy/k8s").mkdir(parents=True)
    (tmp_path / "docs/runbooks").mkdir(parents=True)
    (tmp_path / "deploy/shared-knowledge.yaml").write_text(
        "repository: mrjithendar/moks-sre-agent\nservice: moks-sre-agent\nsources:\n"
        "  - {doc_type: runbook, include: ['docs/runbooks/**/*.md']}\n  - {doc_type: manifest, include: ['deploy/**/*.yaml']}\nexclude: ['deploy/shared-knowledge.yaml']\n")
    (tmp_path / "docs/runbooks/payments.md").write_text("---\nservice: payments\nseverity: sev2\n---\n# Payments crash loop\n\n1. Read the logs\n"
                                                         "Use header Authorization: Bearer abcdefghijklmnop1234 to call it.\n")
    (tmp_path / "docs/runbooks/old.md").write_text("---\nstatus: deprecated\n---\n# Old runbook\n")
    (tmp_path / "deploy/k8s/secret.yaml").write_text("apiVersion: v1\nkind: Secret\nmetadata: {name: s, namespace: shop}\nstringData: {password: hunter2}\n")
    (tmp_path / "deploy/k8s/app.yaml").write_text("apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: payments, namespace: shop}\n---\n"
                                                  "apiVersion: v1\nkind: Service\nmetadata: {name: payments, namespace: shop}\n")
    (tmp_path / "README.md").write_text("# not covered\n")
    return shared_ingest.load_config(tmp_path / "deploy/shared-knowledge.yaml")


async def test_sync_ingests_changes_deletes_removed_and_deprecated_runbooks_and_never_sends_a_secret(tmp_path):
    cfg = _repo(tmp_path)
    r = Rag()
    out = await shared_ingest.sync(client_for(r), cfg, tmp_path, changed=["docs/runbooks/payments.md", "docs/runbooks/old.md", "deploy/k8s/secret.yaml", "deploy/k8s/app.yaml", "README.md"],
                                   deleted=["docs/runbooks/gone.md"], revision="abc123")
    actions = {o.path: o.action for o in out}
    assert actions == {"docs/runbooks/gone.md": "deleted", "docs/runbooks/payments.md": "ingested", "docs/runbooks/old.md": "deleted",
                       "deploy/k8s/secret.yaml": "refused", "deploy/k8s/app.yaml": "ingested"}  # README.md is no source's
    deletes = [c["path"] for c in r.calls if c["method"] == "DELETE"]
    assert deletes == ["/documents/moks-sre-agent:docs:runbooks:gone.md", "/documents/moks-sre-agent:docs:runbooks:old.md"]
    sent = {c["body"]["doc_id"]: c["body"] for c in r.calls if c["path"] == "/ingest"}
    rb = sent["moks-sre-agent:docs:runbooks:payments.md"]
    assert rb["title"] == "Payments crash loop" and rb["metadata"] | {"doc_type": "runbook", "service": "payments", "severity": "sev2", "revision": "abc123"} == rb["metadata"]
    assert rb["metadata"]["url"] == "https://github.com/mrjithendar/moks-sre-agent/blob/abc123/docs/runbooks/payments.md"
    assert "abcdefghijklmnop1234" not in rb["text"] and "[REDACTED]" in rb["text"] and not rb["text"].startswith("---")
    mf = sent["moks-sre-agent:deploy:k8s:app.yaml"]["metadata"]
    assert mf["doc_type"] == "manifest" and mf["namespace"] == "shop" and mf["resources"] == "Deployment/payments, Service/payments"
    assert not any("hunter2" in json.dumps(c["body"]) for c in r.calls if c["body"])


async def test_the_command_line_does_a_first_load_and_a_dry_run_sends_nothing(tmp_path, capsys):
    _repo(tmp_path)
    p = argparse.ArgumentParser()
    shared_ingest.add_arguments(p)
    r = Rag()
    assert await shared_ingest.run(p.parse_args(["sync", "--root", str(tmp_path), "--all", "--dry-run"]), client_for(r)) == 0
    assert r.calls == [] and "would_ingest" in capsys.readouterr().out
    assert await shared_ingest.run(p.parse_args(["sync", "--root", str(tmp_path), "--all"]), client_for(r)) == 0  # the Secret is refused, not failed
    assert {c["body"]["doc_id"] for c in r.calls if c["path"] == "/ingest"} == {"moks-sre-agent:docs:runbooks:payments.md", "moks-sre-agent:deploy:k8s:app.yaml"}
    r.fail = 503
    assert await shared_ingest.run(p.parse_args(["delete", "moks-sre-agent:docs:runbooks:payments.md"]), client_for(r)) == 1
    assert await shared_ingest.run(p.parse_args(["delete", "x"]), shared.SharedRag("", "")) == 2


def test_the_repository_source_list_covers_runbooks_and_manifests_and_never_secrets():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    cfg = shared_ingest.load_config(root / shared_ingest.CONFIG)
    files = shared_ingest.every_file(cfg, root)
    assert "docs/OPERATOR_GUIDE.md" in files and "deploy/helm/moks/values-hetzner.yaml" in files
    assert not any(f.startswith("deploy/secrets/") or f.endswith(".example.yaml") for f in files)


def test_a_postmortem_carries_service_severity_incident_and_date():
    inc = SimpleNamespace(id=uuid.UUID(int=7), title="Payments down", severity="sev2", namespace="shop", resource="Deployment/payments", owner_team=None,
                          resolved_at=datetime(2026, 10, 2, 9, tzinfo=timezone.utc), created_at=datetime(2026, 10, 2, 8, tzinfo=timezone.utc),
                          postmortem={"root_cause_verified": True})
    doc = shared_ingest.postmortem_doc(inc, "prod", "# Incident: Payments down\nAuthorization: Bearer abcdefghijklmnop1234", "https://jithendar.com/moks")
    assert doc["doc_id"] == f"moks-postmortem:{inc.id}" and doc["title"] == "Postmortem: Payments down"
    assert doc["metadata"] == {"doc_type": "postmortem", "incident_id": str(inc.id), "service": "payments", "severity": "sev2", "date": "2026-10-02", "environment": "prod",
                               "namespace": "shop", "url": f"https://jithendar.com/moks/incidents/{inc.id}", "root_cause_verified": True}
    assert "abcdefghijklmnop1234" not in doc["text"]


async def test_an_approved_postmortem_is_queued_and_pushed_to_moks_rag(client, rag):
    name = f"pm-{uuid.uuid4().hex[:6]}"
    await make_env(name)
    tag = uuid.uuid4().hex[:6]
    await make_user(f"pm-{tag}@example.com", [f"investigator:{name}:*", f"approver:{name}:*"])
    h = await login(client, f"pm-{tag}@example.com")
    inc = (await client.post("/moks/api/incidents", json={"title": f"Payments down {tag}", "environment": name, "namespace": "shop", "severity": "sev2"}, headers=h)).json()
    await client.put(f"/moks/api/incidents/{inc['id']}/postmortem", json={"impact": "Payments unavailable", "root_cause": "db password rotated"}, headers=h)
    await client.patch(f"/moks/api/incidents/{inc['id']}", json={"status": "resolved"}, headers=h)
    assert (await client.post(f"/moks/api/incidents/{inc['id']}/postmortem/approve", headers=h)).status_code == 200
    async with session_scope() as db:
        job = (await db.execute(select(Job).where(Job.type == "shared_kb_push", Job.payload["incident_id"].astext == inc["id"]))).scalar_one()
        assert job.job_class == "knowledge" and job.status == "queued"
    from moks import jobs

    rag.fail = 503  # moks-rag is down: the job fails with a kind, and the worker tries it again later
    async with session_scope() as db:
        with pytest.raises(shared_ingest.PushFailed) as err:
            await jobs.handlers()["shared_kb_push"](db, job.payload)
    assert err.value.kind == "unavailable"
    rag.fail = None
    async with session_scope() as db:
        out = await jobs.handlers()["shared_kb_push"](db, job.payload)
    body = next(c["body"] for c in rag.calls if c["path"] == "/ingest" and c["body"]["metadata"].get("incident_id") == inc["id"])
    assert out["doc_id"] == body["doc_id"] == f"moks-postmortem:{inc['id']}"
    assert body["metadata"]["severity"] == "sev2" and body["metadata"]["environment"] == name and body["metadata"]["namespace"] == "shop" and body["metadata"]["date"]
    assert "db password rotated" in body["text"] and body["collection"] == "sre_knowledge"
    async with session_scope() as db:
        notes = [e.payload.get("text") for e in (await db.execute(select(IncidentEvent).where(IncidentEvent.incident_id == uuid.UUID(inc["id"])))).scalars()]
    assert any("shared SRE knowledge base" in (n or "") for n in notes)


# ---- alert triage and root-cause analysis ---------------------------------------------------------------------


@pytest.fixture
def fake_k8s(monkeypatch):
    monkeypatch.setitem(registry.CONNECTOR_TYPES, "kubernetes", FakeKubernetes)
    registry._cache.clear()
    FakeKubernetes.delay = 0.0
    FakeKubernetes.calls = []
    yield
    registry._cache.clear()


@pytest.fixture
def hook(monkeypatch):
    monkeypatch.setattr(get_settings(), "alert_webhook_token", "hook-secret-123")
    return {"Authorization": "Bearer hook-secret-123"}


ALERT = {"alerts": [{"status": "firing", "labels": {"alertname": "KubePodCrashLooping", "severity": "critical", "namespace": "shop", "pod": "payments-5c4-xyz", "container": "app"},
                     "annotations": {"summary": "Pod shop/payments-5c4-xyz is crash looping",
                                     "description": "Container app of pod payments-5c4-xyz restarted 7 times: CrashLoopBackOff, exit code 1"},
                     "startsAt": "2026-11-26T10:00:00Z"}]}


async def _alert_investigation(client, hook) -> uuid.UUID:
    name = f"sk-{uuid.uuid4().hex[:6]}"
    env_id = await make_env(name, connectors=[{"type": "kubernetes", "name": "k8s", "config": {"mode": "in_cluster"}}])
    async with session_scope() as db:
        e = await db.get(Environment, env_id)
        e.auto_investigate, e.alert_cooldown_minutes = True, 30
        from moks.api.environments import sync_automation_binding

        await sync_automation_binding(db, name, True)
    alert = json.loads(json.dumps(ALERT))
    alert["alerts"][0]["labels"]["run"] = uuid.uuid4().hex[:6]  # its own fingerprint
    d = (await client.post(f"/moks/api/alerts/webhook/{name}", json=alert, headers=hook)).json()["decisions"][0]
    assert d["decision"] == "investigated", d
    iid = uuid.UUID(d["investigation_id"])
    await Investigator(iid, "test-worker").run()
    return iid


async def _result(iid: uuid.UUID) -> tuple[Investigation, list[Evidence]]:
    async with session_scope() as db:
        inv = await db.get(Investigation, iid)
        ev = (await db.execute(select(Evidence).where(Evidence.investigation_id == iid, Evidence.tool == "shared_kb_search"))).scalars().all()
    return inv, sorted(ev, key=lambda e: int(e.ref[1:]))


async def test_an_alert_is_triaged_with_the_shared_runbooks_and_past_incidents(client, hook, rag, fake_k8s):
    iid = await _alert_investigation(client, hook)
    inv, ev = await _result(iid)
    assert inv.status == "completed"
    # The hybrid query MOKS built: alert name, workload and namespace first, then the exact codes, then (refresh) the error lines triage found.
    assert rag.queries[0].startswith("KubePodCrashLooping payments namespace shop pod payments-5c4-xyz container app")
    assert "CrashLoopBackOff" in rag.queries[0] and "exit code 1" in rag.queries[0]
    assert len(rag.queries) == 2 and "db timeout" in rag.queries[1]  # refresh: the logs added an error line the alert did not have
    assert all(c["headers"]["authorization"] == f"Bearer {TOKEN}" and c["headers"]["x-service-name"] == "moks-sre-agent" for c in rag.calls)
    assert all(c["body"]["include_answer"] is False and c["body"]["collection"] == "sre_knowledge" for c in rag.calls)
    assert len(ev) == 2 and {e.kind for e in ev} == {"documentation"} and all(e.ok for e in ev)
    sk = inv.report["shared_knowledge"]
    assert sk["status"] == "ok" and [s["stage"] for s in sk["searches"]] == ["initial", "refresh"]
    assert sk["runbooks"][0]["doc_id"] == RUNBOOK["doc_id"] and "CrashLoopBackOff" in sk["runbooks"][0]["matched"]  # exact codes outrank rag-api's first
    assert sk["incidents"][0]["metadata"]["incident_id"] == "inc-41"
    assert all(p["doc_id"] != ELSEWHERE["doc_id"] for p in sk["incidents"]) and sk["searches"][0]["filtered_out"] == 1  # another environment's postmortem
    steps = inv.report["next_steps"]
    runbook_step = next(s for s in steps if "Runbook **Payments crash loop**" in s)
    assert "(1) Read the previous container's logs" in runbook_step and "kubectl -n shop rollout undo deploy/payments" in runbook_step
    assert "Documented, not observed" in runbook_step and ev[-1].ref in runbook_step
    incident_step = next(s for s in steps if s.startswith("Past incidents"))
    assert "[Postmortem: payments db timeout](https://jithendar.com/moks/incidents/inc-41)" in incident_step and "2026-10-02, sev2, payments" in incident_step
    assert not any("Operator guide" in s for s in steps)  # a runbook with no procedure adds no step
    assert not any("moks-rag" in m for m in inv.report.get("missing_evidence") or [])


@pytest.mark.parametrize("fail", ["connect", "timeout", 503, 401])
async def test_triage_carries_on_with_live_evidence_when_moks_rag_is_down(client, hook, rag, fake_k8s, fail):
    rag.fail = fail
    iid = await _alert_investigation(client, hook)
    inv, ev = await _result(iid)
    assert inv.status == "completed" and inv.report["findings"]  # the alert, the logs and the cluster still answer
    sk = inv.report["shared_knowledge"]
    assert sk["status"] == "failed" and sk["runbooks"] == [] and all(s["status"] == "failed" for s in sk["searches"] if s.get("ref"))
    assert ev and not any(e.ok for e in ev)
    assert any("moks-rag" in m and "alert, logs and cluster evidence" in m for m in inv.report["missing_evidence"])
    assert any("could not be searched" in n for n in inv.report["notes"])
    assert not any("Runbook **" in s for s in inv.report["next_steps"])


async def test_nothing_is_sent_and_nothing_is_said_while_moks_rag_is_not_configured(client, hook, fake_k8s, monkeypatch):
    r = Rag()
    monkeypatch.setattr(shared, "TRANSPORT", httpx.MockTransport(r.handler))
    monkeypatch.setattr(shared, "_client", None)
    monkeypatch.setattr(get_settings(), "shared_rag_url", "")
    monkeypatch.setattr(get_settings(), "shared_rag_token", "")
    iid = await _alert_investigation(client, hook)
    inv, ev = await _result(iid)
    assert inv.status == "completed" and r.calls == [] and ev == []
    sk = inv.report["shared_knowledge"]
    assert sk["status"] == "not_attempted" and "not configured" in sk["reason"] and sk["searches"] == []
    assert not any("moks-rag" in m for m in inv.report["missing_evidence"])
