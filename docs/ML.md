# Machine learning in MOKS

MOKS uses ML to suggest things: unusual behavior in workloads and nodes, which of those anomalies belong to one
problem, what happened the last times something similar failed, and which skills fit. ML never grants a permission,
opens an incident, changes a severity or runs an action, and deterministic detection runs exactly as before. Every
capability can be switched off. When it is off, or when its model is missing or invalid, MOKS does what it did
without it.

Code: `backend/moks/ml/`. API: `/moks/api/ml/...`. Schema: migrations 0032 and 0041. UI: **ML insights** (Monitor)
and **Machine learning** (Settings).

```text
 Prometheus ──probe──► features.py ──► detectors.py ──► anomaly.py (train: compare, gate, snapshot, register)
 (app metrics,           catalog v2      7 algorithms         │
  Istio, cAdvisor,       one grid        JSON state           ▼   shadow tick, every 15 min
  kube-state-metrics)    provenance      held-out thresholds  ml_observations ──► correlate.py ──► ml_correlations
                                                                    │              (mesh calls, pods/nodes,
 approved incidents ──► history.py (recurring, what worked)        │               rollouts, events, Loki,
                                                                    ▼               alert history)
                         signals.py ──► ml_anomalies / incident_history tools ──► investigations and the crew
                                        (evidence kind model_output: a lead, never a finding or a confident cause)
```

## Status

| Capability | Built | Checked here (PostgreSQL, fake connectors) | Shadow-tested on a cluster | Validated in production |
|---|---|---|---|---|
| Passage-level citations | yes | yes | no | no |
| Relevance: explicit relationships before the namespace | yes | yes | no | no |
| Readiness counts that match search | yes | yes | no | no |
| Learning records, reviews, judgments, datasets | yes | yes | no | no |
| Similar incidents | yes, **enabled** by default | yes (synthetic incidents) | no | no |
| Anomaly detection: feature catalog v2, 7 detectors, comparison and gates | yes, **disabled** by default, shadow only | yes (synthetic series and the synthetic benchmark) | no | no |
| Node (infrastructure) anomalies | yes, trained separately | yes (synthetic) | no | no |
| Incident correlation | yes, runs after each shadow tick | yes (synthetic, with known answers) | no | no |
| Historical incident intelligence | yes, read-only | yes (synthetic reviewed records) | no | no |
| ML evidence in investigations and the CrewAI crew | yes (`ml_signals`, `incident_history`) | yes | no | no |
| Semantic skill recommendations | yes, **disabled** by default | yes | no | no |
| Supervised skill ranking | **not built**: waits for reviewed outcomes | n/a | n/a | n/a |
| Supervised or deep time-series models | **not built**: waits for labeled incidents (see [Evaluation](#evaluation-and-promotion)) | n/a | n/a | n/a |

Nothing here has run against mk4, real Prometheus, a real embedding model or real incidents. The only evaluation
numbers are from synthetic data written for the tests (see [Benchmark](#benchmark-synthetic)). They show the code
does what it was designed to do, and say nothing about real-world quality.
## Modes and the kill switch

Each capability has a mode in the `ml_capabilities` table. It is read on every use, so a change takes effect on the
next investigation or tick, with no restart.

| Capability | Modes | Default | Shadow | Enabled |
|---|---|---|---|---|
| `similar_incidents` | disabled, shadow, enabled | enabled | computed and stored, not shown | shown in investigations |
| `anomaly_detection` | disabled, shadow | disabled | observations and correlation groups shown on ML insights, offered to investigations as `model_output` evidence, and judged | not offered |
| `skill_recommendations` | disabled, shadow, enabled | disabled | ranking recorded beside the rules' choice | may fill skill places the rules left empty; never removes or reorders |

- **Switch:** Settings > Features (or Settings > Machine learning), or `PUT /ml/capabilities/{name}` with `{mode, reason}`. Needs `ml.admin`. The
  change is audited (`ml.capability`).
- **Kill everything:** set `ml.disabled: true` in Helm values (`MOKS_ML_DISABLED`). Every capability then reads as
  disabled, whatever the database says. Use this when the database itself is suspect.
- **Fail closed:** an unknown capability, or a mode that cannot be read, counts as disabled.

## Roles

| Permission | In | Allows |
|---|---|---|
| `ml.read` | viewer and every role above it | observations, correlation groups (every namespace they span), history and similar incidents within the reader's bindings |
| `ml.feedback` | investigator, operator, `ml_reviewer`, `ml_admin` | judge a passage, similar incident, recommendation, observation or correlation group |
| `learning.review` | `ml_reviewer`, `ml_admin` (scoped) | confirm causes and resolutions; mark judgments reviewed |
| `learning.export` | `ml_admin` (global) | build and export datasets |
| `ml.admin` | `ml_admin` (global) | modes, training, promotion, rollback, rejection, model health and reproduction |

`admin` holds all of these. Scoped reads are filtered row by row, so a reader never sees another scope's rows or
counts. None of these permissions reach infrastructure.

## Learning data

A **learning record** is written when an investigation finishes, in its own transaction after the answer is stored,
so it can never fail the investigation. Another is written when a postmortem is approved. A record holds:

- identifiers and kinds, never evidence text;
- scope: environment, namespace, service and resource;
- the time window and recent rollouts;
- passage ids by state;
- skills that were eligible, recommended and executed, with versions and step outcomes;
- model, feature-schema and pipeline versions.

Error messages are kept as normalized signatures: identifiers, numbers, addresses and paths become placeholders, and
the text is redacted.

The rules:

- *Completed* is not *resolved*. `outcome.resolution` stays `unknown` until a reviewer confirms it or the
  postmortem is approved.
- An executed skill is not a successful one. `skills.executed[].outcome` says only whether its steps ran
  (`steps_ran`, `steps_failed`, `not_run`, `stopped`). Whether it helped is a judgment someone records.
- Labels come from a reviewer (Settings > Machine learning > Confirm outcomes) or an approved postmortem. Nothing is
  labeled automatically.
- Judgments are one per person per target and context. A changed verdict goes back to `open`, and nothing uses a
  judgment until a reviewer marks it `reviewed`.
- Evaluation runs (`trigger=eval`) are written in the `evaluation` realm and never mix with operational records.
- Retention: `ml.learningRetentionDays` (365). The daily `learning_purge` job, or `moks ml purge`, deletes expired
  records and datasets. Approved incident records have no expiry.

A **dataset** (`POST /ml/learning/datasets`, `learning.export`) is a frozen, versioned selection:

- It includes approved records only. Which ones depends on the purpose: `incident_similarity` reads incidents whose
  postmortem was approved; `skill_ranking`, `retrieval` and `anomaly` read any approved record, including outcomes
  confirmed under Confirm outcomes. Records from evaluation runs build only from the `evaluation` realm.
- `GET /ml/learning/datasets/readiness` counts what a build would find: approved records per purpose and realm, and
  how many wait for review. The Build dataset drawer shows these counts, starts on a purpose that has records, and
  says what to approve when the chosen one has none. A build that finds nothing answers 422 with the same advice.
- Duplicates by content hash are dropped first.
- Every record of one incident, deployment or cited document family lands on one side of the split. `chronological`
  puts the latest groups in evaluation.
- MOKS refuses to save a dataset if any group or content hash would appear in both train and evaluation.
- Export is JSONL with features and labels only, redacted again, and is audited.
- **Contents are frozen at build time.** Each item keeps the redacted record exactly as exported, with its SHA-256.
  The checksum (`checksum_version` 2) covers the export schema and fields, the purpose, realm, filters and split
  settings, and every item's record id, split, group and payload hash. Export reads the snapshot, never today's
  records, and refuses (409) if the snapshot no longer matches the checksum. One `name@version` and checksum
  therefore always mean the same rows. The response carries `X-MOKS-Dataset-Checksum` and `X-MOKS-Dataset-Provenance`.
- **When sources change.** A label edited while the record stays approved does not touch existing versions; build
  a new version to take it in. A label withdrawn (approved to rejected or unreviewed) or a record deleted (retention
  or its investigation deleted) **invalidates** every dataset holding it: a database trigger (migration 0035) sets
  `state = invalidated` with the reason and clears that dataset's snapshot, and export answers 410. Item rows stay
  as references (there is no longer a foreign key that would silently drop them). A dataset's own `expires_at`
  deletes it with its snapshot.
- **Legacy datasets** (built before 0035, `checksum_version` 1) have no snapshot. They are listed as
  `legacy_live_records`, not reproducible: their export reads today's records, says `"provenance": "live"` on each
  row, and their checksum covers record ids and splits only. Rebuild them to get a frozen version.

Nothing in MOKS trains on conversations or on feedback automatically.

## Similar incidents

Candidates are approved incidents (postmortem approved) in the reader's realm. Access is checked first, per
incident: the reader must hold knowledge access and `ml.read` in the incident's environment and namespace, and the
incident must be inside the request's scope. The comparison has two parts:

- **Structure:** symptoms (finding kinds), error-signature tokens, service, resource kind and whether a rollout
  preceded both, compared as sets. The weights are in `ml/similar.py`.
- **Text:** the existing knowledge search, limited to the `incident-postmortems` source. It uses embeddings when the
  active index has them, and keyword search otherwise.

If the text search fails, structure alone still ranks, and the result says so. With keyword-only text it says
"embeddings unavailable".

In an investigation this is one bounded call after triage, through the executor (tool `similar_incidents`, evidence
kind `documentation`), with its own one-call allowance. Each result shows:

- what matched, what differs, and how long ago it happened;
- whether the cause was verified, the cause, and the resolution;
- a link to the incident.

The section always says that historical similarity does not show the current cause. Claim validation treats this
evidence like documentation, so it never stands as an observation, and a cause backed only by it stays low
confidence.

Existing approved postmortems: run `moks ml index-incidents` once in the worker pod, so incidents approved before
this release get records.

## Anomaly detection (shadow)

### Data sources

Everything is read through connectors MOKS already has, as the automation principal, through the executor. No new
collector, agent or exporter is installed.

| Source | Used for | Read with |
|---|---|---|
| Prometheus: application metrics | request rate, errors, latency, queue depth, database connections | `prom_query` |
| Prometheus: Istio sidecar telemetry (`istio_requests_total`, `reporter="destination"`) | the same traffic features when the application exports none; the call graph | `prom_query` |
| Prometheus: cAdvisor (kubelet) | CPU, memory, throttling, network errors; node CPU and memory (root cgroup) | `prom_query` |
| Prometheus: kube-state-metrics | restarts, node pressure (absent on mk4, and reported as absent) | `prom_query` |
| Kubernetes | pod placement and owners, Deployment revisions (rollouts, deployment frequency), warning events | `k8s_list_pods`, `k8s_rollout_history`, `k8s_events` |
| Loki | error-line volume per workload for a group's window | `loki_query` |
| MOKS: alert history | alerts received for a group's workloads | `alert_events` |
| MOKS: approved incidents and reviewed investigations | evaluation labels, masking known incidents out of training, history | `learning_records` |
| Git | not read by ML; investigations still read it through `recent_commits` and `deployed_commit` | |
| OpenTelemetry traces | not read directly: MOKS has no trace connector. Call relationships come from the mesh telemetry, which records the same client and server pairs a trace would | |

### Feature catalog (schema `metrics-anomaly/2`)

One grid for training, validation and scoring (`ml.anomaly.stepSeconds`, 300 s), from `features.align_window`: both
ends floored to a multiple of the step in UTC, the window half-open `[start, end)`, chunks on the grid, and each
timestamp requested exactly once. A window shorter than one step is `insufficient_window`, never 0% coverage.

| Feature | Unit | Level | Source | Missing means |
|---|---|---|---|---|
| `request_rate` | req/s | service | app, else mesh (pinned per cohort) | no request metric |
| `error_ratio` | ratio | service | app, else mesh | no request metric, under 0.05 req/s, or the error query was incomplete |
| `latency_p50`, `latency_p95`, `latency_p99` | s | service | app, else mesh (ms converted) | no duration histogram or no traffic |
| `cpu_usage` | cores | resource | cAdvisor | no series |
| `cpu_utilization` | ratio | resource | cAdvisor, containers with a CPU limit only | no container has a limit |
| `cpu_throttling` | ratio | resource | cAdvisor | no CFS counters |
| `memory_working_set` | bytes | resource | cAdvisor | no series |
| `memory_utilization` | ratio | resource | cAdvisor, containers with a memory limit only | no container has a limit |
| `restart_rate` | 1/h | resource | kube-state-metrics, raw counter with resets handled | no kube-state-metrics |
| `network_errors` | 1/s | resource | cAdvisor (receive + transmit) | no series |
| `queue_depth` | messages | service | a configured gauge (`ml.anomaly.queueDepthMetric`) | not configured or not exported |
| `db_connections` | connections | service | a configured gauge (`ml.anomaly.dbConnectionsMetric`) | not configured or not exported |
| `node_cpu_utilization` | ratio | infrastructure | cAdvisor root cgroup / `machine_cpu_cores` | no series |
| `node_memory_utilization` | ratio | infrastructure | cAdvisor root cgroup / `machine_memory_bytes` | no series |
| `node_pressure` | flag | infrastructure | kube-state-metrics node conditions | no kube-state-metrics |

Deployment frequency is not a grid feature: Kubernetes keeps only the last revisions of a Deployment, so it is
reported per correlation group (rollouts in the last 24 hours and 7 days, labeled a lower bound).

Rules the code enforces, each with a test:

- **Missing is not zero.** A feature with no series is `absent` (with the reason), a gap is `missing`, and neither is
  filled. Zero is inferred only where a definition says absence means zero (`errors_rate`: no 5xx series with
  traffic means no errors), and only inside a chunk that completed.
- **Partial and failed collection.** Every chunk of every query is `success`, `failed`, `partial` (warnings,
  truncation, the 25-series cap) or `absent` (the probe found no series, so it was not queried); counts are in
  `data_quality.chunks`. A value in a failed or partial chunk is unknown.
- **Sampling intervals.** Before the chunked reads, one probe per base metric counts samples per series over the last
  hour. Counters and histograms use `rate()` over one window (`ml.anomaly.rateWindowSeconds`, never below the step);
  a counter sampled less often than a quarter of that window is `undersampled` and not used. Gauges are summarized
  over the step (`max_over_time`), so each grid point covers the whole step whatever the exporter's interval; a
  gauge slower than the step simply has gaps.
- **One source per cohort.** Training pins the traffic source (`app` when it covers half the window, else `mesh`);
  inference reads only the pinned one, so a model is never scored on another measurement of the same thing.
- **Provenance.** Each frame records, per feature, the source, raw queries, exact PromQL, native interval, rate
  window, chunk outcomes and coverage, plus the catalog digest. Models keep it with their training record;
  observations keep a compact copy. A change to the catalog or to the configured metric names changes the digest,
  and older models are reported `stale_schema` rather than scored on a different measurement.

### Detectors

All seven are fitted on training data only, store plain JSON (artifacts refuse anything else), and report per
feature the observed value, the baseline, the deviation and the direction.

| Algorithm | Kind | Baseline |
|---|---|---|
| `robust_zscore` | univariate | median and MAD of the training period |
| `rolling_zscore` | univariate | mean and standard deviation of the trailing 3 hours |
| `ewma` | univariate | exponentially weighted mean and variance; an outlier moves it by at most 4 spreads |
| `seasonal_baseline` | univariate | median and MAD per hour of week, then hour of day; only for features with a daily cycle (the others keep one median) |
| `seasonal_decomposition` | univariate | trailing 24-hour trend (anomalies clipped out of it) plus an hour-of-day profile; scores the residual |
| `isolation_forest` | multivariate | isolation depth over the usable features |
| `local_outlier_factor` | multivariate | local density against 256 training rows |

- **Levels.** Metric level: each flagged feature. Service level: a workload's window, univariate or multivariate.
  Infrastructure level: node cohorts (`env/_node/<node>`). Multivariate: Isolation Forest and LOF.
- **Thresholds** are calibrated per feature on held-out training data (fitted on the first 75%, the 99.5th
  percentile of sustained deviations on the last 25%, never below 3), then the state is refitted on all of it.
  Calibrating on the data a model was fitted on made the hour-of-week model alarm on 30% of healthy windows in the
  benchmark; held-out calibration brought that to about 1%.
- **Persistence.** A feature is flagged only when it stays past its threshold for `ml.anomaly.persistPoints`
  consecutive points (2), so one odd sample is not an anomaly.
- **Direction.** Each feature says which way is a problem: latency, errors, memory, restarts and node pressure only
  when higher, traffic and database connections both ways. Deviations the other way are still reported, not flagged.
  A multivariate flag whose clearly moved features all moved the harmless way is not raised.
- **No look-ahead.** Online detectors read only points before the one they score. A test changes later values and
  expects identical earlier scores.

### Training, comparison and promotion gates

`moks ml train --environment E --namespace N` (or `--nodes`), Train models in Settings, or the weekly CronJob. For
each cohort with at least `minHistoryDays` (7) of usable history:

1. **Split** chronologically at 80%; the first rate window after the cut (the embargo) is not judged.
2. **Mask** approved incidents in the training period (30 minutes before to their end, or 2 hours), so a known
   incident is not learned as normal. Only approved incidents; nothing unreviewed, and no investigation output.
3. **Freeze** the training data as a content-addressed snapshot (`ml_artifacts`, media type
   `application/vnd.moks.ml-dataset+json`). Its SHA-256 is the model's dataset version (`prometheus#<sha>@<from>..<cutoff>`).
4. **Fit every algorithm** from the decoded snapshot, then fit it again and compare the state digests.
5. **Judge** each one on the validation period three ways: alarms per day as it was (an upper bound on false alarms
   if that period was healthy), recall and delay on approved incidents in it, and recall, precision, F1 and delay on
   six faults injected into it (spikes, level shifts, drifts, sized by that workload's own spread). Every algorithm
   gets the same faults, and the current champion is judged on the same windows.
6. **Register** each as a `candidate` with its gates and the recommended one (passes its gates, best F1 on the
   injected faults, fewest alarms). Nothing is promoted automatically.

| Gate | Passes when |
|---|---|
| `history` | usable history ≥ `minHistoryDays` |
| `validation_windows` | ≥ 12 tick windows judged |
| `flags_per_day` | alarms (runs of consecutive flagged ticks) per day on the validation period ≤ `maxFlagsPerDay` (3) |
| `feature_coverage` | ≥ 2 usable features (1 for a node) |
| `synthetic_recall` | ≥ `minSyntheticRecall` (0.5) of the injected faults found |
| `detection_delay` | median delay on them ≤ `maxDetectionDelayMinutes` (45) |
| `reproducible` | refitting from the snapshot gives the same state |
| `incident_recall` | only with ≥ 3 approved incidents in the validation period: recall ≥ 0.5 |

Deep or supervised time-series models are deliberately not built: there are no labeled incidents on mk4 to train or
judge them with. Once `incident_recall` can be measured on enough approved incidents, a supervised model can be
compared through the same pipeline and gates.

### Shadow inference

The `ml_anomaly_tick` job, per environment with an active model, at most every `intervalMinutes` (15). For each
namespace it reads the last `windowMinutes` (60), plus history only when an active model needs it (rolling 3 h,
EWMA 6 h, decomposition 24 h, capped by `lookbackHours`). It scores the champion and any challengers and writes
`ml_observations`, then regroups the environment's anomalies, and nothing else.

An observation is `scored`, or `insufficient_history`, `no_telemetry`, `maintenance` (scored for the record, never
flagged), `model_invalid` (artifact checks failed) or `stale_schema` (trained on another feature schema or catalog).
A scored, flagged observation carries:

- `anomaly_type` (service, infrastructure or multivariate), `first_flagged_at` and `flagged_features`;
- per feature: observed, typical (the baseline), deviation, threshold, direction and pattern (spike, sustained, rising);
- `confidence`: low, medium or high, with reasons. Medium needs 1.5× the threshold on well-covered features; high
  also needs another model flagging the same window and a model whose reviewed precision is ≥ 0.7 on ≥ 20 judged
  flags. Input drift (PSI > 0.25) or incomplete queries make it low. It ranks a lead; it is not a probability;
- `validation`: `unvalidated` (< 5 reviewed verdicts), `partially_validated`, `validated` or `failed_validation`, the
  gates, the recall on injected faults, and the mode, which is always shadow.

## Correlation

After each tick, `correlate.py` groups the environment's recent champion anomalies (`ml.correlation.lookbackMinutes`,
120) so one problem becomes one thing to investigate rather than one alert per metric.

- An **anomaly** is one cohort's run of flagged ticks (gaps under two intervals are one run).
- Two anomalies **link** when first flagged within `ml.correlation.windowMinutes` (30) and tied by enough structure:
  one calls the other (0.6), two calls apart (0.35), both call the same service (0.3), a node and a workload on it
  (0.6), the same node (0.1), a rollout in the hour before that touches both (0.3), the same namespace (0.15), plus up
  to 0.2 for timing. A link needs 0.5, so a shared namespace and good timing alone never join two anomalies.
- **Groups** are the connected components. Each gets candidate explanations, ranked by how many members they
  explain: a node problem, a rollout, the earliest anomaly among the services the others call, a shared dependency
  that was not flagged. Every one is labeled `candidate, unvalidated`.
- **Context** stored with a group: the call edges, pod placement, rollouts and deployment frequency, warning
  events, Loki error-line counts and alert history for its workloads, and every source that could not be read
  (`context.gaps`).
- A group stays `open` while new anomalies join it and is `closed` after `closeAfterMinutes` (60) without one.
- **Access.** A group is shown only to someone with `ml.read` in every namespace it spans (the environment as a whole
  for node members). Similar incidents and history for a group are looked up when it is opened, with the reader's own
  access, and never stored in it.
- Operators judge a group (belong together, partly, wrong grouping, unsure). Judgments are review input like any
  other; nothing learns from them automatically.

## Historical incident intelligence

`history.py` (`GET /ml/history`, the `incident_history` tool, `moks ml history`) summarizes reviewed outcomes only:
approved incidents, investigations a reviewer approved (counted once per incident), and reviewed skill judgments.
Each record is checked with the similar-incidents access rule before it is counted.

- **Recurring failures**: records whose symptoms and error signatures match (Jaccard ≥ 0.5) at least twice, with the
  services, the confirmed causes, how many were verified and the median time to resolve.
- **Repeated infrastructure problems**: eviction, pending pods, image pulls, missing ingress backends, and node,
  volume and ingress resources.
- **Frequently affected services**, with incidents in the last 30 days and the median time to resolve.
- **Runbooks**: runbooks named on approved incidents, documents cited by approved investigations whose resolution
  was confirmed (only those the reader may read), skills reviewers judged helpful.
- **What worked**: resolutions on incidents whose cause was verified.

## In investigations and the CrewAI crew

Two read-only tools, with the target bound by MOKS and never by the model:

| Crew tool | MOKS tool | Agent | Evidence kind |
|---|---|---|---|
| `ml_signals` | `ml_anomalies` | workload investigator | `model_output` |
| `incident_history` | `incident_history` | change investigator | `documentation` |

`ml_signals` shows, for the target workload, each anomaly's type, affected resource, detection time, baseline,
observed value, deviation, model and version, confidence and validation status, and the correlation group it belongs
to with its candidate explanations. `ml.read` is checked per row. With anomaly detection disabled the tool reports a
gap and the crew carries on.

`model_output` is a new evidence kind, and the rules for it are in code:

- Claim validation never accepts a statement resting only on it as an observation: it becomes a hypothesis, "a
  statistical lead from a shadow-mode model, not an observation of the system".
- In the crew's answer, a finding that cites only ML signals is moved to the likely causes at low confidence, and a
  likely cause resting only on them is capped at low ("ML signals are leads, not observations").
- Every agent's backstory carries, whatever the console says, that ML signals are unvalidated leads and history is
  the past, neither a finding nor a confirmed cause.

## Models, versions and governance

- **Artifacts** are canonical JSON, stored by SHA-256: in PostgreSQL by default, or in S3/R2 with
  `ml.artifactStore: s3`. Only MOKS's own training writes them, and there is no upload path.
- **Before every use** MOKS checks the checksum, format, library major version (`moks_ml` 2.0), algorithm, feature
  schema, feature list, sampling step and the state's structure (`detectors.validate`). Any failure falls back to
  deterministic behavior. A pickle is refused because it is not JSON.
- **Versioning.** Model versions per cohort and algorithm; feature schema `metrics-anomaly/2` plus a catalog digest;
  dataset version from the training snapshot's SHA-256; library versions and the app commit in provenance.
- **Reproducibility.** `POST /ml/models/{id}/reproduce` (Models → Reproduce from snapshot, `moks ml reproduce`)
  refits from the stored snapshot with the recorded seed and compares state digests. A changed snapshot is refused.
- **Statuses:** `candidate`, `shadow` (scored as a challenger), `champion` (one per cohort, a unique index),
  `retired`, `rejected`.
- **Promotion** is explicit (Models → Make champion, `POST /ml/models/{id}/promote`) and audited. A champion needs
  passed gates and the capability not disabled. Promoting a new champion retires the old one.
- **Rollback:** `POST /ml/rollback {capability, cohort}`. The champion steps down and the most recently promoted
  retired model returns. If there is none, the cohort shows nothing.
- **Health.** `GET /ml/models/{id}/health` (Models → Health) and hourly in the tick for champions: alarms per day
  against the gate, median input drift (PSI) per feature, judged precision, telemetry gaps, stale schema. It
  recommends a review or a retrain, sets `moks_ml_model_degraded`, and never rolls back on its own.
- **No automatic training on investigation output.** Training reads telemetry; labels come only from approved
  incidents; correlation weights are fixed in code; history reads reviewed records. Nothing retrains from feedback.

## Skill recommendations

Order is fixed:

1. Publication, enablement, environment policy, connectors, telemetry and permissions decide which skills are
   eligible (`skills/selection.py`).
2. The deterministic ranker picks its skills.
3. Only then are the eligible skills ranked by similarity to the symptoms. This uses embeddings when the knowledge
   index has an embedding model, and TF-IDF otherwise.

An operator's chosen skill stays first. In `enabled` mode only skills above similarity 0.18 that the rules did not
pick can fill the remaining places, up to 3 in total. Any error falls back to the rules' choice
(`moks_ml_fallbacks_total{capability="skill_recommendations"}`). The ranking, method and additions are recorded in
the report and the learning record, so they can be judged and evaluated later.

A supervised ranker is deliberately not built: there are no reviewed outcomes yet, and training on executed skills
would teach "it ran" as "it worked". Build it from a `skill_ranking` dataset once enough approved records and
reviewed `skill_recommendation` judgments exist.

## Evaluation and promotion

| What | How | Where |
|---|---|---|
| Retrieval Recall@k, MRR, nDCG | `ml/evaluation.py` `ranking()`; the existing `rag` and `rerank` datasets | `moks eval rag`, `moks eval rerank` |
| Citation correctness and coverage | `citations()` over report `knowledge` sections | offline |
| Similar incidents | `moks eval similar_incidents` (synthetic, with hard negatives); `moks ml evaluate-similar` (reviewed judgments) | eval runs, CLI |
| Skill recommendation usefulness | reviewed `skill_recommendation` judgments | `skill_ranking` datasets |
| Detector comparison: precision, recall, F1, false-positive rate, detection delay, inference latency | `ml/benchmark.py` on synthetic incidents | `moks ml benchmark`, `moks eval anomaly_benchmark` (recorded with gates) |
| The same per workload, on its own data | faults injected into its validation period | training metrics and gates |
| Incident recall and delay on real incidents | approved incidents in the validation period | training metrics; a gate with ≥ 3 |
| Correlation accuracy | pairwise precision and recall of "same incident", adjusted Rand index, groups per incident | `moks ml benchmark` |
| Judged precision in shadow | reviewed `anomaly_observation` judgments | model health, observation validation status |
| Inference latency, fallbacks, missing features, drift, flags, groups | Prometheus metrics below | dashboard |

Measures are event-based, as on-call experiences them. A fault is detected when an alarm (a run of consecutive
flagged ticks) starts between its start and its end plus 30 minutes. Precision is matching alarms over alarms;
alarms on benign changes (rollouts) are counted apart. The false-positive rate is flagged windows over windows that
overlap no fault and no rollout. Delay runs from the fault's start to the end of the first flagged window, which is
when a 15-minute tick would show it.

### Benchmark (synthetic)

`moks ml benchmark` and the `anomaly_benchmark` eval: a small cluster (a front end, two APIs, a database, a queue
worker, two nodes; 14 days at 5 minutes) with daily and weekly cycles, correlated features, telemetry gaps and two
benign rollouts. The last 3 days hold 11 labeled faults in 6 incidents: a latency spike, an error-rate shift after a
rollout, a 9-hour memory leak, a database slowdown that reaches its callers, a node under memory pressure with
restarts on its workloads, and a 70% traffic drop.

Results on seeds 7, 11 and 23 (2026-11-20), means with the F1 range:

| Detector | Kind | Recall | Precision | F1 (range) | FPR | Median delay | Inference per window |
|---|---|---|---|---|---|---|---|
| `robust_zscore` | univariate | 0.88 | 0.94 | 0.91 (0.82–0.95) | 0.001 | 15 min | 0.2 ms |
| `rolling_zscore` | univariate | 0.91 | 0.61 | 0.73 (0.67–0.80) | 0.013 | 15 min | 5.1 ms |
| `ewma` | univariate | 0.76 | 0.81 | 0.77 (0.70–0.84) | 0.005 | 15 min | 0.4 ms |
| `seasonal_baseline` | univariate | 0.94 | 0.63 | 0.74 (0.61–0.81) | 0.014 | 15 min | 0.3 ms |
| `seasonal_decomposition` | univariate | 0.94 | 0.68 | 0.78 (0.71–0.92) | 0.020 | 15 min | 1.1 ms |
| `isolation_forest` | multivariate | 0.61 | 0.59 | 0.60 (0.49–0.67) | 0.010 | 20 min | 0.8 ms |
| `local_outlier_factor` | multivariate | 0.91 | 0.94 | 0.92 (0.91–0.95) | 0.001 | 15 min | 3.1 ms |

Correlation over the best detector's anomalies: pairwise F1 0.95, 1.0 and 1.0 (adjusted Rand 0.94, 1.0, 1.0); over
the true anomalies, 1.0 on every seed.

How to read this:

- The faults are large (several spreads) and the generator is the one these detectors were refined against: the
  direction rule, the robust trend and held-out calibration were added after this benchmark showed false alarms.
  These numbers show the code finds what it was built to find. They are not evidence about real workloads, and the
  per-workload gates exist because of that.
- Delay is set mostly by the 15-minute tick and the two-point persistence rule.
- The memory leak is found hours late by every detector except rolling z-scores and the decomposition: a slow change
  looks like the baseline moving.
- Isolation Forest cannot see a feature that was constant in training (restarts that never happened), and one
  feature's spike is diluted across fourteen. It is kept as a challenger, not recommended.
- The seasonal models' false alarms are mostly memory, which wanders without a daily cycle.

## Setup on mk4

This is yours to run. It writes to mk4, and Argo CD reads `origin/master`.

1. **Ship the code.** Push, wait for the image, then restart the API and worker (`rollout restart deploy/moks-api
   deploy/moks-worker` in `moks-sre`). Migration 0041 runs on start (a new table, an index and a widened check; it is
   additive). The config map gains the new `MOKS_ML_ANOMALY_*` and `MOKS_ML_CORRELATION_*` keys.
2. **Retrain anything trained before.** Models from feature schema 1 are reported `stale_schema` and score nothing.
   Train each namespace again, then the nodes:
   `exec deploy/moks-worker -- moks ml train --environment mk4 --namespace <ns>` and `... --environment mk4 --nodes`.
   Read each cohort's `recommended` model and its gates. On mk4 expect restarts and node pressure to be absent (no
   kube-state-metrics), and traffic to come from the mesh for workloads that export no HTTP metrics.
3. **Optionally configure the application gauges**: `ml.anomaly.queueDepthMetric` and `ml.anomaly.dbConnectionsMetric`
   (gauges with `namespace` and `pod` labels), or leave them absent.
4. **Turn on shadow mode and pick champions**: Settings > Features > anomaly detection > shadow, with a reason. Then
   Settings > Machine learning > Models > Make champion, one per workload and node.
5. **Scheduled training**: `ml.training.enabled: true` and `ml.anomaly.namespaces: "mk4:<ns>,mk4:_node"`. Seven
   algorithms per cohort take longer than two did: watch the first run against `activeDeadlineSeconds` (3600).
6. **Dashboard and alerts**: dashboard MOKS · ML anomaly detection is `moks-ml` in mrjithendar/monitoring (`generators/moks/build.py`); the `moks-ml`
   group in `deploy/grafana/moks-alerts.yaml` is about MOKS's own models (an invalid artifact, a champion to review,
   context that cannot be read), never about workloads. Validate the rules with the Prometheus pod's promtool before
   adding them.
7. **Run the benchmark as an evaluation** if you want it on record: Settings > Evaluation > `anomaly_benchmark`.

## Verify

- An investigation shows **Knowledge used** with "Retrieved n · eligible n · given to the model n · cited n", and
  each cited passage's button reads `E3.2` and opens evidence `E3`.
- An investigation like a past approved incident shows **Similar incidents** with the matched symptoms and the note
  that similarity is not a cause.
- After shadow mode and a champion:
  - `moks_ml_observations_total{status="scored"}` rises; `moks_ml_fallbacks_total{reason="model_invalid"}` stays 0;
  - no incident, detection state or action plan appears that would not have appeared otherwise;
  - ML insights shows observations with a confidence and, once two related workloads are flagged together,
    a **Correlated problems** row whose candidate explanations say unvalidated;
  - a CrewAI investigation of a flagged workload has an `ml_signals` evidence row labeled "ML signal (unvalidated)",
    and no finding cites only it.
- `moks ml reproduce <model id>` answers `"reproducible": true`.
- The kill switch: set anomaly detection to disabled. The next tick logs `anomaly_detection is disabled`, no new
  observations or groups appear, and `ml_signals` reports a gap.

## Roll back

| What | How |
|---|---|
| One capability | set its mode to `disabled` |
| All of ML | `ml.disabled: true`, push, sync, restart API and worker |
| One model | Settings > Machine learning > Models > Roll back this cohort, or `POST /ml/rollback` |
| The release | as in [RELEASES.md](RELEASES.md). Migrations 0032 and 0041 are additive, so the previous image runs on the new schema; it ignores `ml_correlations`. Its anomaly code cannot read moks_ml 2.0 artifacts and reports them `model_invalid` until those cohorts are trained again with it |

## Limitations

- Nothing has been exercised against mk4, real Prometheus, Loki, a real embedding model or real incidents. Every
  feature query, probe, alert and dashboard expression has been parsed by a PromQL parser in the tests, never executed
  by Prometheus. The call-graph and node queries depend on mk4's relabeling (`node` on cAdvisor series, Istio's
  workload labels), read from the kubernetes repository, not checked live.
- The benchmark's numbers come from synthetic data the detectors were refined against (see [Benchmark](#benchmark-synthetic)).
- Traces are not read directly (no trace connector); the mesh call graph stands in for them, and a call between
  workloads that are not in the mesh is invisible to correlation.
- The connector's 25-series cap applies to the call graph too; a busy namespace's graph can be incomplete, and the
  group says so in its gaps.
- On mk4 there is no kube-state-metrics: no restarts and no node pressure. Queue depth and database connections need
  a configured gauge.
- Correlation weights and thresholds are fixed in code, chosen by reasoning and checked on synthetic incidents; they
  have not been tuned on judged groups.
- Pure-Python detectors, chosen so artifacts are data: training seven algorithms on 40 cohorts takes minutes, not
  seconds. LOF keeps 256 reference rows per cohort.
- Similar incidents compares at most `ml.similarIncidents.candidates` (300) of the most recent approved incidents.
- `moks_ml_feature_drift_psi` is labeled by cohort, so its series count grows with `maxCohorts × features`.
