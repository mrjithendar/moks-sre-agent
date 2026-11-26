# Knowledge sources

Knowledge is untrusted reference material. A retrieved document can inform an investigation, but it can
never become a skill, grant a permission, or stand in for live evidence. That holds even for a document
that looks exactly like a `SKILL.md`.

## Who can do what

| Permission | Roles | Allows | Scope |
| --- | --- | --- | --- |
| `knowledge.read` | every role that can read investigations | search, the Explorer, feedback | per binding |
| `knowledge.test` | investigator, operator, `knowledge_reviewer`, `knowledge_admin`, `index_operator` | the retrieval test page | per binding |
| `knowledge.review` | `knowledge_reviewer`, `knowledge_admin`, admin | see drafts and quarantined documents, review, change a document's lifecycle, read feedback | per binding |
| `knowledge.source_admin` | `knowledge_admin`, admin | create, change, sync and retire sources | global only |
| `knowledge.index` | `index_operator`, admin | index generations, outbox, backfill, retries, diagnostics | global only |

Administering a source and running the index are separate on purpose. A reviewer outside their binding gets 404, as for any document they cannot see.

## Who can read what

A document has the environment and namespace of its source. `*` means that axis is unrestricted.

- A person reads a document when **one binding** matches both axes. With `prod/team-a` and `dev/team-b`,
  `prod/team-b` is not readable.
- A filter of whole bindings runs before ranking wherever the backend can express it. PostgreSQL always
  can. Pinecone cannot express patterns.
- Where a filter is incomplete, search widens its candidates within a limit of 400, so unreadable documents
  cannot crowd out readable ones. The response reports `candidates.filtered_before_ranking` and `expanded`.
- Every result is checked again against PostgreSQL. That check is never skipped.
- Asking for an environment or namespace you hold no binding on returns nothing, including shared documents.
- Investigations search only their own environment and namespace.

**Sharing globally.** A source scoped to `*/*` is readable by everyone with any knowledge access, so it must
be chosen with `shared_globally`. Confidential and restricted sources cannot be shared globally. Sources
that were already `*/*` before migration 0021 are recorded as shared, because they already were.

There is no retrieval result cache, so no cache key can outlive a revoked binding.

## Lifecycle

| State | Syncs | Searchable | Notes |
| --- | --- | --- | --- |
| draft | yes | no | Fills up so a reviewer can read it first. |
| active | yes | yes | |
| quarantined | no | no | Stops at once. Documents are kept for review. |
| archived | no | no | |
| deleted | no | no | Tombstones stay until the index has dropped them. The name can be reused. |

A document has its own `draft`, `quarantined`, `active`, `archived` or `deleted` state. `review_required` on a source holds new and changed documents in quarantine for a reviewer. Reviewing a document sets its next review date from the source's interval when no date is given.

`review_interval_days` makes a source stale when its review is overdue, and `stale_after_days` when a
document has not been confirmed against its source. `stale_policy: flag` marks results, and `exclude` drops them.
Example: [examples/knowledge-source.json](examples/knowledge-source.json).

## What a GitHub source reads

A source chooses **profiles** (`config.profiles`, in the source form as "What to index"):

| Profile | Reads |
|---|---|
| `documentation` | Markdown, MDX, reStructuredText, AsciiDoc, text, `docs/**`, `runbooks/**` |
| `infrastructure` | YAML, Helm charts and values, kustomizations, Terraform and HCL, Dockerfiles, CI workflows (GitHub, GitLab, Jenkins), Makefiles, shell |
| `application` | Python, Go, JavaScript, TypeScript, TSX, shell, Dockerfiles, `package.json`, `go.mod`, `pyproject.toml`, `requirements*.txt` |

Explicit `include` globs always count, and profiles add to them. A source with neither reads what every source
read before profiles existed (Markdown, `docs/`, `runbooks/`, YAML, Dockerfile, workflows, Helm, Terraform), so
an existing source is never broadened by an upgrade; application code is read only when someone ticks it.
`exclude` globs always narrow. Whatever a source includes, MOKS never indexes dependencies (`node_modules`,
`vendor`, virtualenvs), generated output (`dist`, `.next`, `__pycache__`, coverage, minified files, source maps,
lock files, Terraform state, generated protobuf), binaries, or files over 300 KB, and refuses files that look
like credentials (key files, `.env`, Secret manifests with data, text with many credential-like values),
recording them as excluded with the reason. `GET /knowledge/profiles` lists the exact patterns.

Every listing records how many files it saw, how many the policy left out and up to 200 of those paths with the
rule that decided, so "why is this file not searchable?" has an answer (see [Source readiness](#source-readiness)).

These always-excluded patterns are a little wider than before this release (virtualenvs, `.next`, coverage,
source maps, more lock files and binary types). A complete listing removes documents that no longer qualify, so
the first sync may delete a few documents of those kinds.

## Synchronization

Sync is daily by default, with Sync Now on the Sources table. Both go through one job, deduplicated per source, and `next_sync_at` is in PostgreSQL, so a restart loses nothing.

Each sync is one bounded pass of 500 files against a persistent inventory (`knowledge_sync_items`).

1. **Listing** pins one commit. A tree GitHub truncates is walked by directory, up to 2,000 requests.
2. **Reading** takes the least recently tried files first, so a file that keeps failing cannot starve others.
3. Another pass is scheduled in 30 seconds until the listing is finished. A new listing starts only then,
   or after 24 hours.

The state is reported separately: listing `complete | partial | failed`, content `in_progress | partial | complete`,
and counts of discovered, processed, excluded, failed and remaining files. **Deletions come only from a
complete listing.** On a web source that means a pass that visited everything: a 404 during an interrupted crawl deletes nothing, because a failing site looks the same. `last_success_at` moves only when both the listing and the content are complete, so stopping at a page limit is not a success.

**Sync is not indexing.** A pass that reads every file finishes the upstream half: `last_sync_status` and
`last_success_at` say so. With PostgreSQL search the chunks are searchable in the same transaction. With
Pinecone the changes wait in the outbox, so the sync result and the Sources table carry a separate
`indexing` state (`complete | pending | failed`, with pending and dead counts). A source is searchable when both
halves are done and its documents are active; see [Source readiness](#source-readiness).

A file PostgreSQL refuses is recorded as failed, and the pass goes on. A page over 300 KB keeps its first 300 KB and says so wherever the document is shown.

`PIPELINE_VERSION` in `rag/chunking.py` is part of change detection. Raise it when chunking, redaction,
extraction or stored metadata change, and GitHub documents are read again even though their content did not change.
Code has its own `CODE_PIPELINE_VERSION`, so a change to how code is split re-reads code files only.

**Upgrading to pipeline 4 (code 5), 2026-11-20:** document types and per-chunk metadata are new for every document,
so the first sync after this release reads every GitHub and web document once more. Text that did not change is not
embedded again: the [embedding cache](#embedding-cache) is filled from the vectors the PostgreSQL index already holds
before that, so only chunks whose text changed (mostly alert rules, manifests, OpenAPI, dashboards and logs, which
are now split differently) call the model. Switching **Split documents by their structure** off and on again does the
same: each setting has its own pipeline versions (3 and 4 for code), so the other setting's documents are read again.
Uploads and postmortems keep no original text, so they stay on their old pipeline until uploaded or approved again. Status shows how many.
Postmortems are typed by the migration; an upload has no type until it is uploaded again, so a type filter does not find it.

## Document types

Every document has a type, decided without a model from its path, its source and its first lines
(`rag/doctypes.py`): `runbook`, `postmortem`, `investigation`, `readme`, `documentation`, `web_page`, `k8s_manifest`,
`helm_chart`, `helm_values`, `helm_template`, `kustomization`, `terraform`, `alert_rules`, `dashboard`, `openapi`,
`ci_pipeline`, `dockerfile`, `code`, `log`, `config`, `text`. A path under `runbooks/`, `playbooks/` or `sops/`, or front
matter `type: runbook`, makes a runbook; `postmortems/`, `incidents/`, `rca/`, or two of the headings Root cause,
Timeline, Impact, Contributing factors make a postmortem. YAML with `groups:` and `- alert:` (or a `PrometheusRule`) is
alert rules; YAML or JSON with `openapi:` or `swagger:` is an API description; JSON with `panels` is a Grafana dashboard.

A type decides how the text is split, which questions it answers best and how it is labelled where it is cited. It
never decides who may read it. Each document also records `resource_types` (the Kubernetes kinds a manifest defines,
the Terraform resource types, `alert`, `dashboard`) and `content_changed_at`, which moves only when its words change,
not when it is read again for a new pipeline. The Explorer filters by type; search and `kb_search` can be narrowed to
types (`doc_type`, `doc_types`) and to documents changed since a time (`changed_since`). Filters only narrow, run before
ranking in PostgreSQL and again in the authoritative check, and an unknown type is refused with the list of known ones.

## Chunks and citations

Chunks are limited by estimated tokens (`MOKS_RAG_CHUNK_TOKENS`, default 350, and `MOKS_RAG_CHUNK_OVERLAP_TOKENS`,
default 30, at 4 characters a token, an estimate because no tokenizer ships with MOKS). With **structured chunking**
(`MOKS_RAG_STRUCTURED_CHUNKING`, on by default, in Settings > Knowledge search) a typed document is split by its
structure (`rag/structure.py`):

| Type | Split | Each chunk says |
|---|---|---|
| alert rules | one chunk per alerting or recording rule; the lines that open a group join its first rule | alert or record name, group, severity, whether it has a `runbook_url` |
| manifests, Helm templates, kustomizations | one section per YAML document, breaking at keys and list items | `Kind namespace/name` |
| OpenAPI | one section per operation and per schema; a JSON description is stored as YAML | `GET /payments`, its `operationId` |
| Grafana dashboards | stored as their panels: title, type, description, row, datasource, queries, unit, thresholds; layout and styling are left out | dashboard and panel |
| logs | a chunk ends only where an event starts, so a stack trace stays with its event | first and last timestamps, levels |
| runbooks, postmortems, investigations | Markdown headings, and a chunk ends between steps, bullets or table rows, not inside one | the section (root cause, timeline, mitigation, diagnosis, ...) and step numbers |
| Terraform | top-level blocks, as before | the resources and modules in it |

A file whose shape a rule cannot read is split by its format as before, and everything else keeps the format rules:
Markdown splits on headings and keeps fences together when they fit, YAML on documents and top-level keys. Code splits at top-level
definitions (Python `def`/`class`, Go `func`/`type`, JavaScript and TypeScript functions, classes, exported
constants, interfaces and types, shell functions, Terraform blocks, Dockerfile stages) once a section holds a
third of a chunk, and each chunk's heading names the definitions in it; otherwise at blank lines. These are
line heuristics, not parsers. A line too long for one chunk is split across several, so nothing is dropped.

Offsets refer to the stored, redacted text. A dashboard or a JSON API description is stored as a rendering, so its lines
are the rendering's: its link names the file without a line anchor, and the citation says `rendered`. Redaction runs on
the original before the rendering. A citation gives source, path, revision and line range.
`revision` is the last commit it was confirmed at, `content_changed_in` the commit its content last changed in. A link always names the same commit as `revision`, including when a file was confirmed unchanged.

Only newlines count as lines, as on a Git host, and redaction keeps every line break, so a citation's lines are the source's. Links have line anchors on Git hosts only, using the plain view for Markdown. Only `http` and `https` links leave MOKS.

## Index consistency

PostgreSQL is the authority. Each change adds an outbox event in its own transaction.

- One worker writes a document at a time (advisory lock). A worker that finds it locked defers.
- If a newer revision committed during a write, the event is recorded as superseded and the newer event rewrites from PostgreSQL.
- `document_index_state` records what each index generation holds.
- A document written while embedding failed has `vector_state: missing`. It is searchable by keyword, shown in
  status, and retried by `backfill_vectors` every minute, stopping at the first failure. The document that failed goes to the back of the queue, so one that always fails cannot hold up the rest. A document with no chunks has nothing to embed, and counts as complete for a strict reindex.
- `reconcile_index` requeues documents whose revisions disagree. `verify_index` compares chunk counts on a sample
  of an external index that can count by metadata (Pinecone cannot, so there it checks nothing and says so).
- Dead events are retried one document at a time or in a bounded batch, and every retry is audited.

Deletion and replacement run write-first: new chunks go in, fenced by content revision, then old ones are pruned, and every bulk item's result is read. `sync_generation` repairs a generation from `document_index_state` alone, which is also how a generation catches up after a cutover or rollback.

## Embedding cache

`embedding_cache` holds one vector per embedding model and exact input text (a chunk's heading and text), keyed by the
text's SHA-256. It holds no text. Every path that embeds goes through it: indexing in PostgreSQL, the outbox for
Pinecone, vector backfill and a rebuild. So:

- a document that changes in one section calls the model for that section (and a chunk overlapping it), not the
  whole document; identical chunks in one document are sent once;
- a rebuild with the same model (after a mapping change, say) reads every vector from the cache;
- the first re-read after a pipeline upgrade costs only the chunks whose text changed, because the worker fills the
  cache from the vectors the active PostgreSQL index already holds, 500 at a time, before that.

`moks_rag_embedding_inputs_total{outcome="computed"|"reused"}` counts both. Vectors of a model no live or recently
retired generation uses are deleted, and so are vectors unused for 30 days whose text no chunk holds any more; a
vector of current text is kept however old. If a model's weights change under the same name, clear its vectors with
`POST /knowledge/index/embedding-cache/clear?model=<model key>` (`knowledge.index`, audited) before the rebuild.

## Retrieval

Results are fused with weighted reciprocal rank fusion from up to four rankings: keyword (PostgreSQL full text), vector, exact identifier, and **named services**: a keyword search for the services the question
names, among those the asker can read about, so "which services depend on redis" also finds manifests that say
`REDIS_HOST` but never "depend". The weights are settings (Settings > Knowledge search, or `MOKS_RAG_WEIGHT_KEYWORD`,
`_VECTOR`, `_IDENTIFIER`, `_ENTITY`, each 0 to 5, with `MOKS_RAG_RRF_K`); 1, 1, 2 and 60 reproduce search before
weights existed exactly, when an exact identifier was listed twice. 0 leaves a ranking out. Every search reports the
`fusion` it used.

After authorization, small visible `signals` adjust the fused order: exact identifier, service, resource type (a
source's `applies_to.resources`, or a manifest defining the kind a question names on its own), a document written
for the scope asked about (or for an environment or namespace the question names), source authority, and from the
question: **document type** (the kind of document its intent is answered by, below), **section** (a postmortem's root
cause or summary for a question about past incidents) and **recent** (content changed within the time the question
asks about). Stale and superseded documents lose a little. Duplicates collapse within a scope. An RRF score only
orders one result list. It is not a probability, and MOKS never presents it as confidence.

`mode` and `fallback_reason` say what actually ran. `evidence.state` is `supported`, `weak` or `none`, decided from what
the passages literally contain. `supported` means the terms are present, not that the answer is right. Search abstains when no identifier from the question appears, when the terms barely overlap, or when every passage that matches is stale or superseded, and it lists the identifiers it could not find.
Conflicts are flagged when two documents share a title and scope, and each result names the other. MOKS does not decide which is right.

Scores order one result list. They are not probabilities, and a citation existing does not show that it supports a claim. `claim_support` checks literal overlap in one passage, and says so. Documents are guidance: what is true of a cluster now needs live tool evidence.

Embedding models, their options, index generations, rebuilding with a new model and rolling back are in [EMBEDDINGS.md](EMBEDDINGS.md).

## Index coverage

`document_index_state` records, per document and index generation, the revision, expected and written chunks and vectors, the model and dimensions, the last error and retries. An index is `complete` or `degraded` from that record, and a degraded one becomes complete only when the record shows every document whole. With Pinecone, written counts are what MOKS recorded, because Pinecone cannot count by metadata.

## Document lifecycle and trust

A document is `draft`, `quarantined`, `active`, `archived` or `deleted`, and only `active` is searched. Search reads this from PostgreSQL, so a quarantine works at commit. `trust` is separate: a sync finding a file makes it `unreviewed`, and changed content loses a review. Only a reviewed document's source authority affects ranking.

Activating a retained generation recomputes its coverage: it comes back `degraded` if it missed anything, and documents' vector state is taken from its own record. Nothing is marked complete because a generation became active. Vector counts come from chunks that really received a vector.

## Knowledge Explorer

**Knowledge > Knowledge Explorer** lists what you can read, with filters by source, scope, service, resource type, document type, owner, team, lifecycle, classification, freshness, indexing and embedding. Sync, indexing and embedding are shown as three states per source, because they finish at different times. A document opens to its sanitized preview, original link, version and citation details, chunks with line and offset ranges, and which index generations hold it.

One predicate in `rag/explore.py` decides what a person sees. The list, each count, every filter option and each direct link use it, so an unreadable document does not appear in a total, does not offer a filter value, and answers exactly like one that does not exist. Excluded, draft and quarantined documents are for reviewers. Retries and errors are for `knowledge.index`.

Everything comes from PostgreSQL. The Explorer never calls an external index, and there is no proxy to one. Embeddings are never shown: only the model, dimensions and coverage. Per-chunk membership is not recorded, so membership is per document, and `verify_index` samples what the index really holds.

## Retrieval test

**Knowledge > Retrieval test**, with `knowledge.test`, compares keyword, vector and hybrid search for one question, and reranking on and off when a reranker is enabled. It runs at most four searches and one embedding, under your own grants. Each column shows what actually ran, which can be less than what was asked for, and why. `knowledge.index` adds candidates and the index name. Token counts are estimates, because embedding providers report none.

Feedback (relevant, irrelevant, outdated, missing information) goes to **Knowledge review** for a person. Nothing reads it to train a model, change ranking or publish anything. It is redacted when stored, and a reviewer sees only what their binding covers.

## Evaluation

Two retrieval datasets run in CI (`tests/test_eval_gates.py`): `rag`, the safety and governance gates, and `sre`
(version 1), retrieval quality over an operations corpus, run twice, as search was before this pipeline and as
configured now. Metrics, results and their limits are in [RAG_EVALUATION.md](RAG_EVALUATION.md).

## Evaluation isolation

Evaluation content lives in `realm = 'evaluation'`, enforced in every candidate query, in the authoritative check and by a composite foreign key that ties a document to its source's realm. No grant reaches across it, including a wildcard admin. A run uses its own backend name, so it never touches an external index or the outbox; it proves isolation before its first case and stops if it cannot. It removes its content in a `finally` block, and `cleanup_evaluations` collects what a dead run left. This is application-enforced in one database, not a separate database or role.

## Query understanding

Search reads identifiers, error names, resource types and time references without a model, and returns them as `understanding`, with typed entities (Kubernetes objects and statuses, AWS ARNs and resource ids, Terraform addresses, metrics, PromQL and LogQL, OpenTelemetry attributes), a question type, and optional SRE synonym expansion: [EMBEDDINGS.md](EMBEDDINGS.md#query-understanding). Normalization never changes an identifier.
With `MOKS_RAG_QUERY_SIGNALS` (on by default) it also reads (`rag/query.py`):

| | From | Used for |
|---|---|---|
| `intents` | rules: past incidents, dependencies, change, procedure, alert, dashboard, API, configuration, troubleshooting; several can match and the first leads | the document-type and section signals |
| `services`, `environments`, `namespaces` | names in the question that match what the asker can read: `payment-service` also names `payments`, `production` names `prod` | the named-services ranking and the service and scope signals |
| `dependents` | for a named service, the services its readable sources declare it a dependency of (`applies_to.dependency_of`), with `dependents_basis` | shown with the answer; it is a declaration, not observed traffic |
| `time_window` | "last hour", "last 3 days", "yesterday", "since 2026-11-01", "recently", and a question about changes with no time (7 days) | the recent signal |

| Intent | Answered best by |
|---|---|
| past incidents | postmortems, investigation history; root cause, summary, impact and resolution sections |
| dependencies, change, configuration | manifests, Helm values and templates, Terraform (and documentation for dependencies, CI for change) |
| procedure | runbooks, documentation, READMEs |
| alert | alert rules, runbooks |
| dashboard / API | dashboards / OpenAPI |
| troubleshooting | runbooks, postmortems, alert rules, documentation |

Names are resolved against one query of the readable catalog (the environments, namespaces and services of documents
the asker's grants can read, under the same filter as search), so understanding can neither name nor hint at anything
the asker cannot read. None of it filters anything or widens a scope: it orders what the grants already allowed. "What
changed in the last hour" is still answered by live evidence (Git and the cluster); knowledge says which documents changed. `MOKS_RAG_DECOMPOSE` splits multi-part questions into separate rankings. It is off: on the `rag` dataset it changed neither recall, MRR nor nDCG and added latency. That dataset is small and synthetic, so re-measure on real questions before turning it on.

## Context assembly

`rag/context.py` lays out retrieved passages for a model, for investigations (`prompt_block`) and for
`POST /knowledge/context` (`knowledge.read`; the same search under the caller's grants, then the block, with
`budget_tokens` and `passage_tokens`). It changes neither what may be read nor the order search chose:

1. **Repeats go:** the same chunk twice, or the same words from another document in the same scope; the dropped one
   names the passage given instead.
2. **Current first:** superseded and deprecated documents follow current ones and say what replaced them, unless the
   question is about past incidents, when history keeps its rank.
3. **One document reads as one piece:** its chunks are grouped where the document first ranked, in the order of the
   text, and chunks that overlap or touch are joined exactly by their offsets, so the overlap is not repeated.
   Search attaches the chunk on each side of the top hits and, for a question about past incidents, the postmortem's
   root cause and summary (the same document, so the same access decision); they join their hit's block without an
   id of their own.
4. **Labels:** document type, freshness and what replaced it, a possible conflict and with what, `unreviewed`, and
   "historical record: what happened then, not how things work now" for postmortems and investigation history.
5. **Budget:** blocks are added until the budget is spent (9,000 characters in an investigation, 1,200 per passage);
   what does not fit is listed as dropped, never cut silently.

Every passage is framed as untrusted data, and the passage ids are the ones the model must cite (`[E3.2]`); a merged
block lists each id in its own bracket.

## Investigation history

With **Search past investigations** on (`MOKS_RAG_INDEX_INVESTIGATIONS`, off by default), finished investigations
become a built-in source, `investigation-history` (type `investigation`, authority -1), synced hourly by the worker:
the question, summary, findings, suspected causes (marked verified or not), verified causes, observed facts and next
steps, redacted like any document and typed `investigation`, so "find similar OOMKilled incidents" reaches what MOKS
found before, not only approved postmortems.

- **Access is the investigation's.** A history document has the investigation's environment and namespace, and
  knowledge reads it under the same single-binding rule investigations are read by. That holds only while every role
  with `knowledge.read` also has `investigation.read`; a sync checks this first and refuses otherwise.
- **Left out:** unfinished or failed runs, evaluation runs, runs older than `MOKS_RAG_HISTORY_DAYS` (180), runs whose
  conversation or question was deleted, and runs of every namespace (`*`), because on a document `*` means
  unrestricted. At most the newest 500.
- **Deleting a conversation or a question removes its history in the same transaction**; every sync also removes,
  from a complete listing, whatever is no longer eligible. Switching it off removes all of it at the next sync.
- It is a historical record: labelled so, unreviewed, ranked below reviewed guidance, and quarantinable in the
  Explorer like any document. It says what was suspected then, never what is true now.

## Knowledge in investigations

Retrieval is a stage of every investigation, not a planner's choice (`orchestrator/knowledge.py`):

    intake -> knowledge -> triage -> logs -> skills -> refresh -> plan/act or rules -> correlate -> report

Triage also searches the shared moks-rag service at the knowledge and refresh stages, as a second source:
[SHARED_KNOWLEDGE.md](SHARED_KNOWLEDGE.md).

- **knowledge** runs before anything live is read, with or without a model. It searches the original question and,
  when it adds anything, a second query built from what MOKS resolved: target, repository, namespace and, for a
  short follow-up such as "how do I fix it?", the words of the turn before.
- **refresh** runs once after triage, only when the findings name symptoms the first queries did not
  (CrashLoopBackOff, OOMKilled, a crash log line) and add at least two new terms.
- Guidance and concept questions use the same stage through `guidance.py`.

Every search is a `kb_search` tool call, so the executor applies RBAC, stores the result as evidence and audits
it. The scope is the investigation's environment and namespace, or the target's namespace once resolved, set
before the first search. The stage has its own allowance, `MOKS_KNOWLEDGE_MAX_SEARCHES` (default 3), counted in
`tool_calls` but not taken from the live-evidence budget. It stops near the time limit. A finished stage is in the
checkpoint and is not run again on resume; a search stored as evidence before a crash is reused, not repeated.
A failed search never stops the investigation: the report says the knowledge gap in `missing_evidence`.

**Relevance never widens access.** It is decided after search, on passages the person may already read. Each
passage gets one authority, with its reasons:

| Authority | When | Used |
|---|---|---|
| `target` | the source's `service` or `applies_to.services` matches the target or a service the question names; or, with no mapping to another service, the repository is the target's, it names a resource from the findings, or it names the target | yes |
| `dependency` | the source declares it in `applies_to.dependency_of` for the target (a database, queue or shared platform the target uses) | yes, labeled as about a dependency, not the target |
| `shared` | documentation with no service binding that shares enough of the request's terms (or ranks top 3 in hybrid search with some overlap), or a source that declares `applies_to.shared: true` | yes, labeled general guidance |
| `unverified` | relevant, but from a repository or service binding not established as the target's, or written for another service while mentioning the target | given to the model with that label; never used for guidance steps |
| `unrelated` | bound to another service | no, with the reason |
| `low_relevance` | shares too little with the request | no, with the reason |

The order is explicit relationships first: the service mapping, a declared dependency, then a mapping to some other
service. Only when nothing explicit decides does the repository name, an identifier or a mention count. **A namespace
is supporting context, never proof**: several services share one, so a runbook scoped to `shop` and mapped to
`billing` stays `unrelated` to `payments` in `shop`, and a namespace-scoped document with no service binding is
`shared`, not `target`. The namespace still shows in the basis. Before 2026-11-05 a namespace match alone made a
passage `target`, ahead of a conflicting service mapping.

Passages are judged again as context grows (triage names the workloads), so a billing runbook found before
triage is marked unrelated once the run knows it is about payments. To make a repository authoritative for a
service, set the source's **Service** (or `applies_to.services`) or the `services` path map.

Passages reach the planner and the report once, laid out by [context assembly](#context-assembly) in a bounded block
(9,000 characters, 1,200 per passage) inside untrusted-data fences, labeled with authority, document type, path, commit,
lines, freshness and any conflict. `kb_search` takes optional `doc_types`, so a plan can ask for alert rules or postmortems only.

**Passage citations.** One `kb_search` is one evidence ref, say `E3`, and holds up to six passages. Each passage
has its own id, `E3.2` (its rank in that search), tied to the chunk, document, revision and line range. The model
is told to cite the passage id. Claim validation maps `E3.2` back to `E3` and keeps the passage beside the claim. If
validation removes the citation, the passage stops counting. Each passage moves through four states:

| State | Meaning |
|---|---|
| `retrieved` | returned by a search the person was allowed to run |
| `eligible` | its authority lets it inform the answer |
| `supplied` | its text was in the prompt; passages past the budget are eligible but not supplied |
| `cited` | the final answer cites it, and the citation survived validation (`cited_by`: model, rules or moks) |

A model may cite only what it was supplied. A citation of any other passage is recorded in
`knowledge.citations.rejected`. A bare `E3` names a passage only when exactly one passage of that search was
available; otherwise it is recorded as ambiguous and no passage is credited. Before this change, citing `E3` marked
every usable passage of the search as used, including passages the model never saw. Reports from before it
(`knowledge.version` absent) have only `used` and `provided`, and the UI derives their state from those. The prompts say documentation
describes intent and never proves current state; claim validation still turns a fact that cites only
documentation into a hypothesis. Without a model, or when the model cites none of the target's passages, MOKS
adds the documented procedures to next steps, labeled "Documented, not observed". Retrieved text grants no tools
and no scope: every call still goes through the executor's allowlist, and remediation is a proposal that needs an
action plan and its approvals.

The report's `knowledge` section (shown as **Knowledge used**) records status (`retrieved | partial | empty |
failed | not_attempted`), the scope searched, every query with its result count, mode and fallback, each passage
with repository, path, commit, lines, link, freshness, authority and why it was or was not used, and, when nothing
was found, hints about the sources the person can read (unfinished syncs, index backlog, documents awaiting
review). Hints count only sources the person can read and never name them. Logs carry stage, outcome, mode,
fallback, timing and counts, never passage text; `moks_knowledge_retrievals_total{stage,outcome}` counts stages.

## Source readiness

`GET /knowledge/sources/{id}/health` (source administrators) separates:

- **upstream**: last successful sync and revision, the pinned revision, and files listed, included, left out by
  policy (with a sample and the rule), excluded on read, failed and pending;
- **documents**: active, draft, quarantined and archived, and vector state;
- **indexing**: backend, mode, outbox pending and dead, documents behind;
- **searchable**: documents and chunks the active backend can return now;
- **retrieval**: `ingested`, `indexed` (at the current revision in the active backend), `eligible` (what search returns
  to someone allowed to read the whole source), `excluded.review | freshness | lifecycle`, `flagged.superseded |
  deprecated | stale` (returned, with a warning), and `operations.pending | failed`. Search and these counts share one
  rule set, `rag/eligibility.py`, applied per row by search and as grouped SQL here. That means a fixed number of
  queries per call, not one per document. Before 2026-11-05 a document whose own review date had passed was dropped
  by search under `stale_policy: exclude` but still counted as searchable here;
- **reasons**, each with a code: `source_inactive`, `never_synced`, `sync_failed`, `sync_incomplete`, `sync_partial`,
  `no_active_index`, `indexing_pending`, `indexing_failed`, `awaiting_review`, `stale_excluded`, `no_searchable_documents`.

With `?path=docs/runbook.md` it says why one file is or is not searchable: `file_excluded` (and the rule),
`file_pending`, `file_failed`, `file_content_excluded`, `file_not_found`, `awaiting_review`, `indexing_pending`.

`POST /knowledge/sources/{id}/test-retrieval` (`knowledge.test`) runs the investigation's search, narrowed to the
source, under the caller's grants and the environment and namespace given. When nothing comes back it adds the
reasons above plus `outside_scope`, `no_access` or `no_results`. Someone who can neither read the source's scope nor
administer sources gets 404; a reader who is not an administrator sees only the scope reasons, not counts. The audit
record keeps the query length, not the query. Both are in the source's details drawer (Retrieval readiness, Test retrieval).

A GitHub knowledge source and a Git connector are different things. The source copies files into the index on a
schedule, and investigations search that copy under each person's knowledge access. The connector reads a
repository live during an investigation (deployed revision, commits, files) within its own allowlist. Neither
creates the other, and neither uses the other's credential.

## Verify end to end

With one GitHub repository and one investigation, on a test environment:

1. **Source.** Knowledge Base, Sources: add a GitHub source for `owner/repo` scoped to the environment and the
   namespace the service runs in, Service set to the service name, profiles Documentation and Infrastructure
   (add Application to index code). The first sync is queued.
2. **Sync.** When the row's Sync shows complete, open Details. Retrieval readiness should read *searchable*, with
   Sync complete, Index complete, and nonzero searchable documents and chunks. If not, the reasons say which half
   is unfinished. With Pinecone, Index may read *indexing* for a minute while the outbox drains.
3. **One file.** Type the runbook's path into "Why is this file not found?" and press Explain: it should say
   Searchable, or name the rule, the review state or the index backlog.
4. **Test retrieval.** In the same drawer, ask what an engineer would ("why is <service> crash-looping") with the
   source's environment and namespace. The runbook's passage should come back with path, lines and commit. Sign in
   as someone without a binding on that namespace and the same test returns 404 or no results with `no_access`.
5. **Investigation.** Run "why is <service> crash-looping?" in that environment and namespace. In the report,
   **Knowledge used** shows status *retrieved*, the scope searched, the queries, and the runbook passage as
   *about the target* with a link to the commit and lines. Recommended next steps cite it (E-ref) as a documented
   procedure, separate from observed facts. The Evidence list shows the `kb_search` call before the first
   Kubernetes call.
6. **Follow-up.** Ask "how do I fix it?" in the same conversation: Queries shows a second query with the service
   name, and the new turn has its own `kb_search` evidence.
7. **Negative check.** Quarantine the runbook document in the Knowledge Explorer and ask again: the passage is
   gone, and Knowledge used gives the reason (nothing relevant, plus a hint that documents await review).
