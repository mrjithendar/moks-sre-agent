# Settings

Everything an administrator manages in the portal is under **Settings**, at the foot of the sidebar
(`/moks/settings`). It has its own submenu, and each page needs its own permission: someone who holds none of them
does not see Settings at all. The old addresses (`/admin/users`, `/admin/ml`, `/audit`, `/evals`, …) redirect to
their Settings page, keeping any query string.

| Group | Page | Needs |
|---|---|---|
| General | Overview: what is on, and links to every page | any page below |
| | Features: ML capability modes, anomaly detection included | `ml.admin` |
| | Limits & schedules: the runtime values below | `admin.settings` |
| | Deployment: every `MOKS_*` value, read-only | `admin.settings` |
| Access | Users & roles | `admin.users` |
| Infrastructure | Environments · Connectors · Backup storage | `admin.policies` · `admin.connectors` · `admin.connectors` |
| AI | LLM providers · Machine learning · CrewAI · Evaluation | `admin.llm` · `ml.admin`, `learning.review` or `learning.export` · `crewai.admin` · `admin.policies` |
| Compliance | Audit log | `audit.read` |

The `admin` role holds all of these. ⌘K finds every Settings page by name or by what it holds ("anomaly", "budgets").

## Features: turning on anomaly detection

Anomaly detection is **disabled** by default and supports **disabled** and **shadow** only: in shadow it scores
workloads and nodes, groups related anomalies into correlated problems and offers them to investigations as leads, and
never opens an incident, changes a severity or runs an action ([ML.md](ML.md)). Shadow mode shows nothing until a
workload has an active model, so:

1. Settings → Features → anomaly detection → **Train models** for a namespace, or for the nodes (needs 7 days of
   history by default). Seven algorithms are trained and compared; each cohort recommends one.
2. Settings → Machine learning → Models: make a candidate that passed its gates **champion** (Health and Reproduce
   from snapshot are in the same menu).
3. Settings → Features → anomaly detection → **Change mode** → shadow, with a reason (audited).

`MOKS_ML_DISABLED=true` (`ml.disabled` in Helm) forces every capability off whatever is saved here.

## Limits & schedules: values that change without a deploy

Helm sets every `MOKS_*` value at start-up. A small, curated set can also be changed here (`moks/runtime_settings.py`):

| Group | Setting | Kind |
|---|---|---|
| Investigations | `auto_investigations_per_hour`, `knowledge_max_searches` | limit |
| | `intent_llm_classifier` | switch |
| LLM spend | `llm_user_daily_budget_usd`, `llm_investigation_budget_usd` (empty = no limit) | limit |
| Knowledge search | `rag_query_expansion`, `rag_require_embedding_evaluation` ([EMBEDDINGS.md](EMBEDDINGS.md)) | switch |
| | `shared_rag_enabled`, and `shared_rag_top_k` (empty = rag-api's default) ([SHARED_KNOWLEDGE.md](SHARED_KNOWLEDGE.md)) | switch |
| Machine learning | `ml_anomaly_interval_minutes`, `ml_similar_incidents_k`, `ml_learning_retention_days` | schedule |
| | `ml_learning_capture` | switch |
| CrewAI | `crewai_timeout_seconds`, `crewai_max_iterations`, `crewai_max_tool_calls` | limit |

- **A limit can be tightened, never loosened:** its Helm value is the most it may be set to. Raising one is still a
  Helm change, reviewed like any other. Schedules and switches move freely within the bounds in code.
- **Every change needs a reason** and is audited (`settings.update`, `settings.reset`, with the old and new values).
  Saving the Helm value, or **Use the Helm value**, removes the saved value.
- **Where it applies:** the API applies a change at once; the worker, the executor and other API replicas apply it
  when they next report their build (within 30 seconds). Code keeps reading `get_settings()`; the values are put on
  each process's settings object. If the table cannot be read, a process keeps what it applied last.
- **A saved value that no longer fits** (Helm lowered a ceiling after it was saved) is ignored, shown as such, and
  the Helm value applies.
- Values stored in the `runtime_settings` table (migration 0037): the API reads and writes it, every other
  component may only read it. API: `GET /api/admin/settings`, `PUT /api/admin/settings/{key}` with
  `{value, reason}`, `POST /api/admin/settings/{key}/reset` with `{reason}`.

Not editable here, on purpose: hard ceilings (`max_tool_calls`, `max_duration_seconds`, …), anything that changes
who may sign in or what MOKS may reach (auth, OIDC, connectors' credentials, secret sync policy), terminal values
(the terminal gateway has its own), and anything read only at start-up (worker pools, rate limiters).

## LLM providers: budgets

Every LLM call reserves its worst-case cost first and must fit **every** limit below (`moks/llm/budget.py`). Days
and months are UTC calendar periods, the same ones Anthropic's spend limits reset on. Spend counts finished calls
plus calls still in progress. Nothing here is preset: the monthly budget and both totals start empty, which means no
limit.

| Limit | Where | Empty |
|---|---|---|
| Daily budget of one LLM | the LLM's form, **Budget** | required (0 allows only free models) |
| Monthly budget of one LLM | the LLM's form, **Budget** | no monthly limit |
| Daily and monthly total of a provider (all its LLMs together, e.g. one Anthropic account's credit) | **Provider totals** card, below the list | no limit |
| Per person per day, per investigation | Limits & schedules → LLM spend | no limit |

The list shows each LLM's spend today and this month against its limits, and the totals card a month-end forecast
(the month's average daily spend so far, over the whole month). Spend at 80 % of a limit or more is highlighted.
A refused call says which limit stopped it, how much was used and when it resets, for example: *The monthly total for
all anthropic LLMs is reached: $100.00 of $100.00 used this month. It resets at 2026-12-01 00:00 UTC; an
administrator can change it in Settings → LLM providers.*

When the provider itself refuses for money (an Anthropic Console spend limit: HTTP 400 "You have reached your
specified API usage limits"; the usage tier's monthly cap: HTTP 429 `enforced_spend_limit_reached`; a billing
problem: HTTP 402), the call fails the same way, as a budget refusal with Anthropic's own message, not as a bad
request or a rate limit. Setting the provider's monthly total to the Console's spend limit keeps MOKS from reaching it.

API: `GET /api/admin/llm/totals`, `PUT /api/admin/llm/totals/{provider}` with `{daily_budget_usd, monthly_budget_usd}`
(`null` = no limit; both `null` removes the totals; audited as `llm.total.update`). `monthly_budget_usd` is a field
of `POST`/`PUT /api/admin/llm`.

## Credentials

API keys, tokens and passwords are pasted into the form that uses them, stored encrypted, and listed here with where
each is used. **Import from Kubernetes Secret** moves existing `MOKS_CRED_*` values in. See
[CREDENTIALS.md](CREDENTIALS.md).

## Deployment: the rest, read-only

Every other `MOKS_*` value, grouped, as this API process started with it, filterable by name or value. Secrets
(every field marked as one, and `MOKS_DATABASE_URL`) show only **set** or **not set**, and credentials inside any
URL are masked. Terminal and secret-sync values are the API's copy; the terminal gateway and the executor get their
own from Helm.
