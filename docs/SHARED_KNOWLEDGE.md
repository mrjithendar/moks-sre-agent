# Shared SRE knowledge (moks-rag)

MOKS triages alerts and writes root-cause analyses against live evidence. With the shared Hybrid RAG service
[moks-rag](https://github.com/mrjithendar/moks-rag) (namespace `rag`, collection `sre_knowledge`) it also reads the
runbooks, past postmortems and k3s/Helm/Istio manifests every MOKS service shares, and puts what matches into the
report: the runbook's steps and links to past incidents, labelled as documentation and history, never as what is
happening now.

MOKS's own knowledge index ([KNOWLEDGE.md](KNOWLEDGE.md)) is unchanged and still searched first. moks-rag is a second
source, searched only for triage.

## Configuration

| Variable | Where it comes from | Meaning |
|---|---|---|
| `RAG_API_URL` | Secret `moks-rag-client`, key `RAG_API_URL` | `http://rag-api.rag.svc.cluster.local:8000` on mk4 |
| `RAG_BEARER_TOKEN` | Secret `moks-rag-client`, key `RAG_BEARER_TOKEN` | rag-api's shared token (`rag/rag-secrets`) |
| `RAG_COLLECTION` | `moks-config` (`sharedKnowledge.collection`) | `sre_knowledge` |

The names carry no `MOKS_` prefix: they are the names every moks-rag client uses (`moks/config.py` reads them
under `shared_rag_*`). Nothing is sent unless the URL and the token are both set. Settings > Knowledge search has two
runtime values: **Search the shared SRE knowledge base** (off: nothing is sent) and **Shared knowledge passages per
search** (empty: rag-api's own default).

Every request carries `Authorization: Bearer <RAG_BEARER_TOKEN>` and `X-Service-Name: moks-sre-agent`. MOKS asks
`POST /query` for passages only (`include_answer: false`): the answer is MOKS's own, under its claim validation, and
moks-rag's model is never called from MOKS.

## Alert triage and root-cause analysis

`orchestrator/shared_knowledge.py` runs for investigations an alert or a detection started, investigations attached
to an incident, CrewAI console flows and any troubleshooting request. Guidance and concept answers keep to MOKS's
index.

| Stage | When | Query |
|---|---|---|
| `initial` | LangGraph's knowledge node, before anything live is read | the alert: name, namespace, workload, pod, container, the codes and lines in its summary and description |
| `refresh` | LangGraph's refresh node, after triage and logs | the same, plus the target's error lines and codes that triage found; skipped when they add nothing |
| `final` | after the CrewAI or unified engine (they have no knowledge node) | everything the run collected about the target |

At most two searches per investigation, from the stage's own allowance: they never take a call from the live
evidence budget. MOKS builds every query itself, never a model; the CrewAI incident crew's knowledge agent also has
`shared_runbooks`, which takes symptoms and error lines from the agent and binds the namespace and workload itself.

A query looks like:

    KubePodCrashLooping payments namespace shop pod payments-5c4-xyz container app | CrashLoopBackOff exit code 1 |
    Pod shop/payments-5c4-xyz is crash looping | ... | level=error msg="db timeout"

The error lines are normalised (timestamps, ids and addresses removed, each line once, secrets redacted), and only
lines about the alert's workload count: another pod's `OOMKilled` stays out of a `payments` query.

**Exact identifiers first.** rag-api fuses dense (bge-m3) and sparse (BM25) rankings with RRF and takes no weights. In
triage the literal strings matter most, so MOKS puts them at the front of the query, where BM25 matches them, and then
orders the passages by how many of them each one contains, with rag-api's order breaking ties:

- service names: the workload and the `service`/`app` label;
- Kubernetes reasons: `CrashLoopBackOff`, `OOMKilled`, `ImagePullBackOff`, `FailedScheduling`, `Evicted`, ...;
- exit codes and signals: `exit code 137`, `SIGKILL`;
- errno, gRPC and HTTP codes: `ECONNREFUSED`, `DEADLINE_EXCEEDED`, `HTTP 503`; exception classes: `OutOfMemoryError`;
- phrases: `no healthy upstream`, `connection refused`, `context deadline exceeded`, `x509: certificate`, ...

A passage that names none of them is kept, ranked after those that do, and shown as "ranked by meaning".

**Scope.** The collection serves every MOKS environment with one token, so MOKS applies its own: a passage whose
metadata names another environment, or a namespace the person may not read, is left out, and the report counts what
was left out. Passages without an environment or namespace are general guidance.

**What the report gets.** Each search is a `shared_kb_search` tool call through the executor: authorized, audited,
redacted, truncated and stored as evidence of kind `documentation`, which can never back an observed fact. Then:

- LangGraph's planner and report writer see the search among their evidence before they write;
- **Recommended next steps** get up to two runbooks' steps and the past incidents, each with the search's evidence id:

      Runbook **Payments crash loop** from the shared SRE knowledge base (E16; matches payments, CrashLoopBackOff):
      (1) Read the previous container's logs; (2) Check the database connection pool;
      (3) `kubectl -n shop rollout undo deploy/payments`. Documented, not observed: check each step against the live
      evidence before following it.
      Past incidents in the shared SRE knowledge base (E16): Postmortem: payments db timeout (2026-10-02, sev2,
      payments). History, not the current cause: ...

- the report's **Shared SRE knowledge** panel lists runbooks, past incidents and manifests with what each matched, and
  the queries (`payload.shared_knowledge`).

## When moks-rag is down

Nothing fails because moks-rag does. The client (`rag/shared.py`):

- has one 10-second deadline per call, covering the connection, the request and one retry made only when the
  connection itself failed (nothing was sent);
- turns every failure into a kind: `unavailable`, `timeout`, `auth`, `bad_request`, `bad_response`, `not_configured`;
- stops asking for 30 seconds after three failures in a row, so an outage costs one wait, not one per investigation.

The failed search is stored as failed evidence, the report's missing evidence says the triage rests on the alert,
logs and cluster evidence only, and the investigation finishes as it would without moks-rag. Metrics:
`moks_shared_rag_calls_total{operation,outcome}`, `moks_shared_rag_seconds{operation}` and
`moks_knowledge_retrievals_total{stage="shared_*"}`.

## Ingestion

Documents go into `sre_knowledge` with a stable `doc_id`, so sending one again replaces it (rag-api drops a
`doc_id`'s earlier chunks first), and a deprecated or deleted one is removed with
`DELETE /documents/{doc_id}?collection=sre_knowledge`. A `doc_id` never contains `/` (rag-api could not route its
DELETE): `<repository name>:<path with : for />`, for example `moks-sre-agent:docs:OPERATOR_GUIDE.md`.

| Kind (`metadata.doc_type`) | From | Metadata |
|---|---|---|
| `runbook`, `architecture` | Markdown in Git | `repository`, `path`, `revision`, `url`; front matter `service`, `severity`, `environment`, `namespace`, `owner` |
| `manifest` | k3s/Helm/Istio YAML in Git | the same, plus `resources` (Kind/name) and `namespace` when every resource shares one |
| `postmortem` | approved MOKS postmortems, or Markdown with front matter | `service`, `severity`, `incident_id`, `date`, `environment`, `namespace`, `url` |

Never sent: Kubernetes `Secret` manifests, anything under a `secrets/` directory, `.env` files and keys. Every text
is redacted first.

**From Git.** [deploy/shared-knowledge.yaml](../deploy/shared-knowledge.yaml) lists what this repository shares.
`status: deprecated` (or `deprecated: true`) in a runbook's front matter deletes it at the next sync.

    export RAG_API_URL=http://127.0.0.1:8000 RAG_BEARER_TOKEN=...     # kubectl -n rag port-forward svc/rag-api 8000
    moks shared-kb status
    moks shared-kb sync --all --dry-run                               # what a first load would send
    moks shared-kb sync --all                                         # the first load
    moks shared-kb sync --changed docs/runbooks/payments.md --deleted docs/runbooks/old.md
    moks shared-kb ingest --doc-type manifest --repository mrjithendar/k8s-deployments --root ../k8s-deployments deploy/minikube/istio/edge/values.yaml
    moks shared-kb delete moks-sre-agent:docs:runbooks:old.md

(`python -m moks.cli shared-kb ...` from `backend/`.) The command exits 1 when anything failed, so CI shows it.

**From CI.** [.github/workflows/shared-knowledge.yml](../.github/workflows/shared-knowledge.yml) runs the sync on every
push to master that touches `docs/` or `deploy/`, with the files the push added, changed, renamed and deleted; run it
by hand with *all* for the first load. It does nothing until the repository has variable `RAG_INGEST_URL` (rag-api as
a GitHub runner reaches it, for example `https://jithendar.com/rag`) and secret `RAG_BEARER_TOKEN`. It never runs on
pull requests.

**Postmortems.** Approving a postmortem queues a `shared_kb_push` job in the same transaction (class `knowledge`,
four attempts with backoff), which sends it with its metadata and notes it on the incident. While moks-rag is down
the job is retried; `moks shared-kb postmortems` sends every approved one again (a backfill).

## Setting it up on mk4

1. rag-api already admits namespace `moks-sre` (`rag-api-callers` AuthorizationPolicy in moks-rag
   `deploy/k8s/istio-routing.yaml`). MOKS must run in `moks-sre` with its sidecar, or the call is refused with 403.
2. Copy the token into MOKS's namespace (a Secret cannot be read across namespaces):

       kubectl -n moks-sre create secret generic moks-rag-client \
         --from-literal=RAG_API_URL=http://rag-api.rag.svc.cluster.local:8000 \
         --from-literal=RAG_BEARER_TOKEN="$(kubectl -n rag get secret rag-secrets -o jsonpath='{.data.RAG_BEARER_TOKEN}' | base64 -d)"

   ([deploy/secrets/moks-rag-client.example.yaml](../deploy/secrets/moks-rag-client.example.yaml).) When the token is
   rotated in `rag-secrets`, update this Secret too.
3. `values-hetzner.yaml` turns `sharedKnowledge` on: api, worker and crewai get the two variables by `secretKeyRef`
   (optional: without the Secret they start and triage on live evidence alone), and the NetworkPolicy allows egress to
   `rag:8000`. Restart the three Deployments after creating the Secret, since environment variables are read at start-up.
4. Check from a pod: `kubectl -n moks-sre exec deploy/moks-api -- moks shared-kb status` should say
   `"reachable": true, "ready": true`.
5. Load the documents: `moks shared-kb sync --all` from a checkout (step "From Git"), then `moks shared-kb postmortems`.
6. Fire a test alert, or open an investigation on an incident: its report has a **Shared SRE knowledge** panel, and
   the evidence list has `shared_kb_search` entries.
