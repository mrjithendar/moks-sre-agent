"""Runs the CrewAI workload investigation inside an Investigator, in place of the LangGraph graph.

    workload investigator -> change investigator -> incident reviewer      (Process.sequential)

Investigator._investigate calls run() instead of graph.run() when the queued request asks for this flow.
Everything after that (claim validation, cause verification, relevance, the stored report, the reply in
the conversation) is the same code every investigation passes.

Threads. CrewAI runs its agents' model and tool calls synchronously in a worker thread, even under
akickoff() (checked against crewai 1.15.44). MOKS's database session, its providers and its executor
belong to the event loop, so each call is handed back to the loop (Bridge) and the thread waits for it.
Nothing CrewAI does touches the database or the network itself.

Cancellation. CrewAI has no way to interrupt a step that is running. When the investigation is cancelled,
times out or hits a limit, the Bridge is closed: the call in flight is cancelled on the loop, and every
later model or tool call the thread attempts raises at once, so the crew stops at its next step.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
import time
from decimal import Decimal
from typing import Any

from opentelemetry import context as otel_context, trace as trace_api
from pydantic import ValidationError

from ..config import get_settings
from ..observability import CREWAI_RUNNING, CREWAI_TOKENS, mark_failed, span
from ..orchestrator import prompts
from ..orchestrator.report import parse_json_object, rules_report
from ..tools.executor import ToolExecutor
from . import CrewUnavailable
from . import config as crew_config
from .evidence import Registry, cited, validate
from .history import History
from .tools import BY_NAME, NEEDS_MEMBERS, NOBODY, CrewTool, Empty, Members, Refused, Target, bind, controllers_for, focus, mine, owned_by

log = logging.getLogger("moks.crewai")

AGENTS = {"workload": "Workload investigator", "change": "Change investigator", "review": "Incident reviewer"}  # the code's default roles
MIN_TOKENS = 200
REFRESH_SECONDS = 60  # how stale the target's pod list may be before a pod it does not know makes MOKS read it again

_lock = threading.Lock()
_running = 0


class CrewStopped(Exception):
    """Raised inside the crew's thread once the run is over (cancelled, timed out or out of budget)."""


async def _slot(wait: float) -> bool:
    """At most crewai_max_concurrent crews per worker process. A counter, not an asyncio.Semaphore,
    because one process can see several event loops (tests, the embedded worker)."""
    global _running
    deadline = time.monotonic() + wait
    while True:
        with _lock:
            if _running < get_settings().crewai_max_concurrent:
                _running += 1
                CREWAI_RUNNING.set(_running)
                return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.2)


def _release() -> None:
    global _running
    with _lock:
        _running = max(0, _running - 1)
        CREWAI_RUNNING.set(_running)


class Bridge:
    """Runs a coroutine on the investigation's loop from the crew's thread and waits for it."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.loop_thread = threading.get_ident()
        self.closed = threading.Event()
        self.reason = "stopped"
        self._inflight: set[concurrent.futures.Future] = set()
        self._guard = threading.Lock()
        self.posted: list = []

    def run(self, coro, timeout: float) -> Any:
        if threading.get_ident() == self.loop_thread:
            coro.close()
            raise RuntimeError("a CrewAI call reached MOKS on the event loop thread; it would deadlock")
        if self.closed.is_set():
            coro.close()
            raise CrewStopped(self.reason)
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        with self._guard:
            self._inflight.add(fut)
        try:
            return fut.result(timeout)
        except concurrent.futures.CancelledError:
            raise CrewStopped(self.reason) from None
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise TimeoutError(f"no answer within {timeout:.0f}s") from None
        finally:
            with self._guard:
                self._inflight.discard(fut)

    def post(self, coro) -> None:
        """Fire and forget, from either thread. CrewAI calls task callbacks on the loop under akickoff()."""
        if threading.get_ident() == self.loop_thread:
            self.posted.append(self.loop.create_task(coro))
        else:
            self.posted.append(asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, self.loop), loop=self.loop))

    async def flush(self) -> None:
        if self.posted:
            await asyncio.gather(*self.posted, return_exceptions=True)
            self.posted.clear()

    def cancel_inflight(self) -> None:
        """Ends the call in flight without closing the Bridge: one step ended, the run goes on (crew/incident.py)."""
        with self._guard:
            for fut in list(self._inflight):
                fut.cancel()

    def close(self, reason: str) -> None:
        if not self.closed.is_set():
            self.reason = reason
            self.closed.set()
        with self._guard:
            for fut in list(self._inflight):
                fut.cancel()


class Run:
    """One crew run: its target, limits, evidence registry and the calls the adapters make."""

    def __init__(self, inv: Any, executor: ToolExecutor, ctx: Any, provider: Any, cfg: Any, target: Target, question: str, loop: asyncio.AbstractEventLoop,
                 crew: dict[str, Any] | None = None, history: History | None = None):
        self.s = get_settings()
        self.inv, self.ctx, self.provider, self.cfg, self.target, self.question = inv, ctx, provider, cfg, target, question
        self.crew = crew or crew_config.merge(None)  # the configuration this run uses (moks/crew/config.py)
        self.limits = self.crew["limits"]
        self.labels = {k: a["role"] for k, a in self.crew["agents"].items()}
        self.history = history or History(inv.id)
        self.executor = ToolExecutor(executor.db, ctx, next_ref=inv._next_ref, timeout_seconds=self.s.crewai_tool_timeout_seconds)
        self.bridge = Bridge(loop)
        self.registry = Registry(excerpt_chars=self.s.crewai_excerpt_chars)
        self.tool_limit = min(self.limits["max_tool_calls"], self.s.max_tool_calls)
        self.tool_calls = 0
        self.llm_calls: dict[str, int] = {}
        self.llm_limit = self.limits["max_iterations"] + 2  # CrewAI's own max_iter, plus the answer it forces at the end
        self.seen: dict[str, str] = {}
        self.refused: list[dict[str, Any]] = []
        self.progress: list[dict[str, Any]] = []
        self.task: str | None = None
        self.task_span: Any = None
        self.root_context = otel_context.get_current()
        self.task_context = self.root_context
        self.members: Members | None = None
        self.members_at = 0.0
        self.resolution: dict[str, dict[str, Any]] = {}  # evidence read to resolve the target, by call key
        self.views: dict[str, Any] = {}  # each registered evidence id's result as the crew sees it: narrowed to the target when namespace-wide

    # ---- progress and tracing (called from the crew's thread) --------------------------------

    def enter(self, agent: str) -> None:
        """The first model call of a task marks its start: CrewAI has no task-start callback. The check
        for cancellation between tasks is the Bridge refusing the next call."""
        if agent == self.task:
            return
        if self.task_span is not None:
            self.task_span.end()
        previous, self.task = self.task, agent
        self.task_span = trace_api.get_tracer("moks").start_span(f"crewai task {agent}", context=self.root_context, attributes={"moks.crewai.agent": agent})
        self.task_context = trace_api.set_span_in_context(self.task_span, self.root_context)
        self.progress.append({"agent": agent, "label": self.labels[agent], "status": "running"})
        self.bridge.run(self._entered(agent, previous), 10)

    async def _entered(self, agent: str, previous: str | None) -> None:
        h = self.history
        await h.finish(previous or "setup", "succeeded")
        await h.start(agent, self.labels[agent])
        tools = self.crew["agents"][agent].get("tools")
        h.line(f"{self.labels[agent]} started" + (f" with {len(tools)} tool(s): {', '.join(tools)}" if tools else ""), data={"agent": agent})
        await self.inv._emit("phase", {"phase": f"crewai_{agent}", "label": f"CrewAI: {self.labels[agent]}"})

    def finished(self, agent: str) -> None:
        for p in self.progress:
            if p["agent"] == agent:
                p["status"] = "done"
        self.bridge.post(self._finished(agent))

    async def _finished(self, agent: str) -> None:
        self.history.line(f"{self.labels[agent]} finished", step=agent)
        await self.history.finish(agent, "succeeded")
        await self.inv._emit("note", {"message": f"CrewAI: {self.labels[agent]} finished"})

    def end_spans(self, outcome: str) -> None:
        if self.task_span is not None:
            self.task_span.set_attribute("moks.outcome", outcome)
            self.task_span.end()
            self.task_span = None

    # ---- the model ------------------------------------------------------------------------

    def complete(self, agent: str, system: str, turns: list[dict[str, str]]) -> str:
        self.enter(agent)
        used = self.llm_calls[agent] = self.llm_calls.get(agent, 0) + 1
        if used > self.llm_limit:
            raise CrewStopped(f"{self.labels[agent]} reached its iteration limit")
        wanted = self.limits["report_tokens"] if agent == "review" else self.limits["step_tokens"]
        return self.bridge.run(self._complete(agent, system, turns, wanted), self.limits["timeout_seconds"])

    async def _complete(self, agent: str, system: str, turns: list[dict[str, str]], wanted: int) -> str:
        from ..llm import budget

        inv = self.inv
        if inv.guard is not None:
            await inv.guard.check()  # nothing more goes to the provider for someone who lost access
        h, model, n, started = self.history, f"{self.cfg.provider}/{self.cfg.model}", self.llm_calls.get(agent, 0), time.monotonic()
        step = self._step_for(agent)
        try:
            with span("crewai llm", context=self.task_context, **{"moks.crewai.agent": agent, "moks.llm.provider": self.cfg.provider}) as s:
                room = inv._capacity(self.cfg, len(system) + sum(len(t["content"]) for t in turns), wanted, MIN_TOKENS)
                resp = await budget.complete(self.provider, system, turns, json_mode=False, max_tokens=room, user_id=inv.owner, investigation_id=inv.id, purpose="investigation")
                s.set_attribute("moks.llm.tokens_in", resp.tokens_in)
                s.set_attribute("moks.llm.tokens_out", resp.tokens_out)
        except Exception as exc:
            ms = int((time.monotonic() - started) * 1000)
            why = getattr(exc, "message", None) or type(exc).__name__
            call_id = await h.call(step, provider=self.cfg.provider, model=self.cfg.model, system=system, turns=turns, response="", tokens_in=0, tokens_out=0, cost=0,
                                   duration_ms=ms, outcome="cancelled" if isinstance(exc, asyncio.CancelledError) else "error", error=str(why))
            h.line(f"Model call {n} failed after {ms / 1000:.1f}s: {why}", "error", {"agent": agent, "model": model, "seconds": round(ms / 1000, 2)}, step=step, call_id=call_id)
            raise
        ms = int((time.monotonic() - started) * 1000)
        inv.tokens_in += resp.tokens_in
        inv.tokens_out += resp.tokens_out
        inv.cost += Decimal(str(resp.cost_usd))
        CREWAI_TOKENS.labels(agent, "input").inc(resp.tokens_in)
        CREWAI_TOKENS.labels(agent, "output").inc(resp.tokens_out)
        h.count(step, model=True, tokens_in=resp.tokens_in, tokens_out=resp.tokens_out, cost=resp.cost_usd)
        call_id = await h.call(step, provider=self.cfg.provider, model=self.cfg.model, system=system, turns=turns, response=resp.text, tokens_in=resp.tokens_in,
                               tokens_out=resp.tokens_out, cost=resp.cost_usd, duration_ms=ms)
        h.line(f"Model call {n}: {resp.tokens_in} in, {resp.tokens_out} out, ${Decimal(str(resp.cost_usd)):.4f}, {ms / 1000:.1f}s",
               data={"event": "llm_call", "agent": agent, "model": model, "tokens_in": resp.tokens_in, "tokens_out": resp.tokens_out, "cost_usd": str(resp.cost_usd),
                     "seconds": round(ms / 1000, 2), "max_tokens": room}, step=step, call_id=call_id)
        await inv._emit("llm_usage", {"tokens_in": inv.tokens_in, "tokens_out": inv.tokens_out, "cost_usd": str(inv.cost)})
        return resp.text

    # ---- tools ----------------------------------------------------------------------------

    def call_tool(self, name: str, raw: dict[str, Any]) -> str:
        tool = BY_NAME[name]
        self.enter(tool.agent)
        return self.bridge.run(self._call_tool(tool, raw), self.s.crewai_tool_timeout_seconds + 15)

    async def resolve_members(self) -> Members:
        """The target's pods by owner reference, read through the ToolExecutor like any other call (so they are
        evidence too): the ReplicaSets the Deployment owns (k8s_rollout_history checks the Deployment's UID), the Jobs
        a CronJob owns, or the StatefulSet, DaemonSet, Job or ReplicaSet itself by UID (k8s_workloads), then the
        namespace's pods those controllers own. A Pod target is that pod alone."""
        t = self.target
        if t.kind == "Pod":
            pods = await self._read("k8s_list_pods", {"namespace": t.namespace})
            if pods is None or not pods.ok:
                return Members(frozenset(), frozenset(), f"the pods could not be read ({(pods.error if pods else None) or 'not run'})")
            found = next((p for p in pods.data or [] if p.get("name") == t.workload), None)
            return Members(frozenset({t.workload}) if found else frozenset(), frozenset(), "" if found else f"there is no pod {t.workload} in {t.namespace}",
                           frozenset({found["node"]}) if found and found.get("node") else frozenset())
        if t.kind == "Deployment":
            out = await self._read("k8s_rollout_history", {"namespace": t.namespace, "deployment": t.workload})
            if out is None or not out.ok:
                return Members(frozenset(), frozenset(), f"its ReplicaSets could not be read ({(out.error if out else None) or 'not run'})")
            owners = frozenset(("ReplicaSet", r["replicaSet"], r.get("uid")) for r in (out.data or {}).get("revisions") or [] if r.get("replicaSet"))
        else:
            out = await self._read("k8s_workloads", {"namespace": t.namespace})
            if out is None or not out.ok:
                return Members(frozenset(), frozenset(), f"the workloads could not be read ({(out.error if out else None) or 'not run'})")
            owners, why = controllers_for(t, out.data or [])
            if not owners:
                return Members(frozenset(), owners, why)
        if not owners:
            return Members(frozenset(), owners, "it owns no ReplicaSets")
        pods = await self._read("k8s_list_pods", {"namespace": t.namespace})
        if pods is None or not pods.ok:
            return Members(frozenset(), owners, f"the pods could not be read ({(pods.error if pods else None) or 'not run'})")
        names = frozenset(p["name"] for p in pods.data or [] if owned_by(p, owners))
        nodes = frozenset(p["node"] for p in pods.data or [] if p.get("name") in names and p.get("node"))
        return Members(names, owners, "" if names else "no pod's owner references point at it", nodes)

    async def _read(self, tool: str, args: dict[str, Any]) -> Any:
        """A read for resolving the target, counted against the crew's tool limit like any other. The first one is
        ordinary crew evidence (the crew's own identical call then answers "already collected"); a repeat, after the
        workload may have been recreated, reads current state again through the same ToolExecutor."""
        key = tool + json.dumps(args, sort_keys=True)
        if self.tool_calls >= self.tool_limit:
            self.registry.gap("limits", f"the crew's limit of {self.tool_limit} tool calls was reached")
            self.history.line(f"Tool-call limit of {self.tool_limit} reached while resolving the workload's pods", "warning")
            return None
        self.tool_calls += 1
        self.history.count(self.history.step, tool=True)
        self.history.line(f"Resolving the workload's pods by owner reference: {tool}", "debug", {"tool": tool, "args": args})
        if key not in self.seen and key not in self.resolution:
            out = await self.inv._call(self.executor, tool, args, "crewai: resolve the workload's pods by owner reference")
            if out is not None and out.evidence_ref:  # registered, narrowed to the target, when the crew asks for it
                self.resolution[key] = next(e for e in reversed(self.inv.evidence) if e["ref"] == out.evidence_ref)
            return out
        return await self.executor.run(tool, args)

    async def _members_for(self, tool: CrewTool, raw: dict[str, Any]) -> Members:
        if tool.name not in NEEDS_MEMBERS:
            return self.members or NOBODY
        named = raw.get("pod") or raw.get("object_name") if isinstance(raw, dict) else None
        unknown = named and named != self.target.workload and named not in self.members.pods if self.members is not None else False
        # again when a pod is not known yet, since the workload may have been recreated; at most once a minute, so
        # naming pods that are not the target's cannot make the crew read the namespace over and over
        if self.members is None or (unknown and time.monotonic() - self.members_at > REFRESH_SECONDS):
            self.members, self.members_at = await self.resolve_members(), time.monotonic()
        return self.members

    def _step_for(self, agent: str) -> str:
        """The console step a call is counted under: the agent in the workload crew, the task in the incident crew."""
        return agent

    def _register(self, record: dict[str, Any], agent: str, members: Members) -> dict[str, Any]:
        view = focus(self.target, record["tool"], record.get("data"), members) if record.get("ok") else None
        entry = self.registry.add(record, agent=agent, focus=view)
        self.views[entry["id"]] = view if view is not None else (record.get("data") if record.get("ok") else None)
        return entry

    async def _call_tool(self, tool: CrewTool, raw: dict[str, Any], agent: str | None = None) -> str:
        h = self.history
        agent = agent or tool.agent
        step = self._step_for(agent)
        shown = {k: v for k, v in (raw if isinstance(raw, dict) else {}).items() if isinstance(v, (str, int, bool)) and v not in ("", None)}
        with span(f"crewai tool {tool.name}", context=self.task_context, **{"moks.crewai.agent": agent, "moks.tool": tool.tool}) as s:
            members = await self._members_for(tool, raw)
            try:
                args = bind(tool, self.target, raw if isinstance(raw, dict) else {}, log_lines=self.s.crewai_log_lines, members=members)
            except Empty as exc:
                self.registry.gap(tool.name, str(exc))
                h.line(f"{tool.name}: empty result, nothing read ({exc})", "warning", {"tool": tool.name, "args": shown})
                return f"Empty result: {exc}. Nothing was read for {tool.name}; record this as missing evidence."
            except ValidationError as exc:
                why = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:3])
                h.line(f"{tool.name}: invalid arguments ({why})", "warning", {"tool": tool.name, "args": shown})
                return f"Invalid arguments for {tool.name}: " + why
            except Refused as exc:
                s.set_attribute("moks.outcome", "refused")
                await self._refuse(tool, raw, str(exc))
                return f"Refused by MOKS: {exc}. This investigation covers only {self.target.kind.lower()} {self.target.workload} in namespace {self.target.namespace}."
            except LookupError as exc:
                self.registry.gap(tool.needs, str(exc))
                h.line(f"{tool.name}: not available ({exc})", "warning", {"tool": tool.name})
                return f"Not available: {exc}. Record this as missing evidence; do not guess."
            if not self.ctx.available(tool.needs):
                self.registry.gap(tool.needs, f"no {tool.needs} connector is configured for environment {self.target.environment}")
                h.line(f"{tool.name}: not available (no {tool.needs} connector in {self.target.environment})", "warning", {"tool": tool.name})
                return f"Not available: no {tool.needs} source is configured here. Record this as missing evidence; do not guess."
            key = tool.tool + json.dumps(args, sort_keys=True)
            if key in self.resolution and key not in self.seen:
                self.seen[key] = self._register(self.resolution[key], agent, members)["id"]
            if key in self.seen:
                entry = self.registry.records[self.seen[key]]
                h.line(f"{tool.name}: already collected as {entry['id']}", "debug", {"tool": tool.name, "evidence": entry["id"]})
                return self._observation(entry, again=True)
            if self.tool_calls >= self.tool_limit:
                self.registry.gap("limits", f"the crew's limit of {self.tool_limit} tool calls was reached")
                h.line(f"{tool.name}: not run, the crew's limit of {self.tool_limit} tool calls was reached", "warning", {"tool": tool.name})
                return "Tool-call limit reached. Finish with the evidence already collected."
            self.tool_calls += 1
            h.count(step, tool=True)
            started = time.monotonic()
            out = await self.inv._call(self.executor, tool.tool, args, f"crewai {self.labels[agent].lower()}")
            ms = int((time.monotonic() - started) * 1000)
            if out is None or not out.evidence_ref:
                self.registry.gap(tool.tool, "not run: " + (self.inv.notes[-1] if self.inv.notes else "the investigation's tool budget is spent"))
                h.line(f"{tool.name}: not run, the investigation's tool budget is spent", "warning", {"tool": tool.name})
                return "Not run: the investigation's tool budget is spent. Finish with the evidence already collected."
            record = next(e for e in reversed(self.inv.evidence) if e["ref"] == out.evidence_ref)
            entry = self._register(record, agent, members)
            self.seen[key] = entry["id"]
            s.set_attribute("moks.evidence", entry["id"])
            h.line(f"{tool.name} ({tool.tool}) → {entry['id']} {'ok' if out.ok else 'FAILED: ' + str(out.error_kind or out.error or 'error')}"
                   f"{', truncated' if entry['truncated'] else ''}, {ms} ms", "info" if out.ok else "warning",
                   {"event": "tool_call", "tool": tool.name, "moks_tool": tool.tool, "args": shown, "evidence": entry["id"], "ok": bool(out.ok), "ms": ms})
            if not out.ok:
                mark_failed(s, "tool_failed", out.error_kind or "error")
            if out.ok and tool.tool == "k8s_triage" and isinstance(out.data, dict):
                for f in out.data.get("findings", []):
                    if mine(self.target, f, members):
                        self.inv.findings.append({**f, "namespace": self.target.namespace, "evidence": [entry["id"]]})
            return self._observation(entry)

    def evidence_list(self) -> str:
        self.enter("review")
        self.bridge.post(self._note(f"evidence_list: {len(self.registry.records)} record(s), {len(self.registry.gaps)} gap(s)", "review"))
        return "Evidence you may cite:\n" + self.registry.listing() + ("\nGaps:\n" + "\n".join(f"- {g['source']}: {g['reason']}" for g in self.registry.gaps) if self.registry.gaps else "")

    async def _note(self, message: str, step: str | None = None, level: str = "info") -> None:
        self.history.line(message, level, step=step)

    async def _refuse(self, tool: CrewTool, raw: Any, reason: str) -> None:
        self.history.line(f"{tool.name}: refused by MOKS ({reason})", "warning", {"event": "refused", "tool": tool.name})
        self.refused.append({"tool": tool.name, "reason": reason})
        self.registry.gap(tool.name, f"refused: {reason}")
        args = raw if isinstance(raw, dict) else {}
        await self.executor.refuse(tool.tool, {k: v for k, v in args.items() if isinstance(v, (str, int, bool))}, f"crewai: {reason}")
        await self.inv._emit("note", {"message": f"Not run: {tool.name} ({reason})"})

    def _observation(self, entry: dict[str, Any], again: bool = False) -> str:
        state = "ok" if entry["ok"] else "FAILED"
        head = f"Evidence {entry['id']} ({entry['source']}, {state}{', truncated' if entry['truncated'] else ''}, collected {entry['timestamp']})"
        if again:
            head = f"Already collected as {entry['id']}. " + head
        return f"{head}. Cite it as {entry['id']}.\n{prompts.untrusted(entry['source'], entry['excerpt'], self.s.crewai_excerpt_chars + 200)}"


def _target_text(t: Target, scope_info: dict) -> str:
    return (f"{t.kind} {t.workload} in namespace {t.namespace}, environment {t.environment}, "
            f"window {scope_info.get('from')} to {scope_info.get('to')} UTC")


async def run(inv: Any, executor: ToolExecutor, ctx: Any, question: str, namespace: str, scope_info: dict, provider: Any, cfg: Any, request: dict[str, Any],
              resumed: dict[str, Any] | None = None) -> dict[str, Any]:
    """Returns graph-shaped state ({"namespace", "report", "mode", "crewai"}) for Investigator._investigate. The incident flow has its own
    workflow (crew/incident.py); `resumed` is a checkpoint's state, which only that flow writes."""
    from . import capability, console, prepare_environment

    if request.get("flow") == crew_config.INCIDENT_FLOW:
        from . import incident

        return await incident.run(inv, executor, ctx, question, namespace, scope_info, provider, cfg, request, resumed)

    s = get_settings()
    history = await History(inv.id).open()
    await history.start("setup", "Set up run")
    try:
        state = capability(s, cfg)
        if not state["available"]:
            raise CrewUnavailable(f"CrewAI investigation unavailable: {state['reason']}")
        crew, used = await console.run_config(request)  # the version (or test draft) recorded when the run was queued
        flow = await console.flow_state(request.get("flow") or crew_config.FLOW)
        if not flow["enabled"]:
            raise CrewUnavailable(f"CrewAI investigation unavailable: the {crew_config.FLOWS[crew_config.FLOW]['label'].lower()} flow is switched off in the CrewAI console")
        if namespace == "*" or not ctx.scope.allows(namespace):
            raise CrewUnavailable(f"CrewAI investigation unavailable: namespace {namespace} is outside this person's scope")
    except CrewUnavailable as exc:
        history.line(str(exc), "error")
        await history.close("failed", str(exc))
        raise
    if ctx.focus_namespace not in (None, namespace):  # a resumed attempt never widens or moves the crew's target
        inv.notes.append(f"The resumed attempt had focused on {ctx.focus_namespace}; the CrewAI investigation stays on {namespace}.")
    ctx.focus_namespace = inv.focus_namespace = namespace  # knowledge retrieval is scoped to the target's namespace
    target = Target(environment=ctx.environment.name, namespace=namespace, workload=request["workload"], kind=request.get("kind") or "Deployment",
                    minutes=max(1, int((ctx.time_to - ctx.time_from).total_seconds() // 60)), repository=inv._repository(ctx))
    limits = crew["limits"]
    history.line(f"CrewAI {state['version']} · model {state['llm']} · configuration: {used}", data={"crewai": state["version"], "llm": state["llm"], "config": used})
    history.line(f"Target: {_target_text(target, scope_info)}")
    history.line(f"Limits: {limits['timeout_seconds']}s, {min(limits['max_tool_calls'], s.max_tool_calls)} tool calls, {limits['max_iterations']} iterations per agent, "
                 f"{limits['step_tokens']}/{limits['report_tokens']} output tokens per step/report", data={"limits": limits})
    await inv._emit("phase", {"phase": "crewai", "label": "CrewAI: waiting for a crew slot"})
    waited = time.monotonic()
    if not await _slot(s.crewai_slot_wait_seconds):
        why = f"CrewAI investigation unavailable: {s.crewai_max_concurrent} crews are already running on this worker; try again shortly"
        history.line(why, "error")
        await history.close("failed", why)
        raise CrewUnavailable(why)
    history.line(f"Got a crew slot after {time.monotonic() - waited:.1f}s")
    crew_run: Run | None = None
    outcome, stopped = "completed", None
    try:
        prepare_environment()
        from . import adapters  # imports crewai, after telemetry is off

        with span("crewai investigation", **{"moks.crewai.version": state["version"], "moks.crewai.workload": target.workload, "moks.namespace": target.namespace}) as root:
            crew_run = Run(inv, executor, ctx, provider, cfg, target, question, asyncio.get_running_loop(), crew=crew, history=history)
            built = adapters.build_crew(crew_run, _target_text(target, scope_info), question)
            raw_text = ""
            try:
                async with asyncio.timeout(limits["timeout_seconds"]):
                    result = await built.akickoff()
                raw_text = str(getattr(result, "raw", "") or "")
            except TimeoutError:
                outcome, stopped = "partial", f"the crew's time limit of {limits['timeout_seconds']}s was reached"
            except asyncio.CancelledError:
                crew_run.bridge.close("cancelled")
                crew_run.end_spans("cancelled")
                await crew_run.bridge.flush()
                history.line("Cancelled", "warning")
                await history.close("cancelled", "cancelled")
                raise
            except Exception as exc:  # a failed provider call, a limit or a CrewAI error ends the crew, not the report
                outcome = "partial"
                stopped = str(exc) if isinstance(exc, CrewStopped) else f"the crew stopped: {getattr(exc, 'message', None) or type(exc).__name__}"
                log.warning("crew stopped", extra={"event": "crewai_stopped", "investigation_id": str(inv.id), "errorType": type(exc).__name__})
            finally:
                crew_run.bridge.close(stopped or "finished")
                crew_run.end_spans(outcome)
                await crew_run.bridge.flush()
            root.set_attribute("moks.outcome", outcome)
            root.set_attribute("moks.crewai.tool_calls", crew_run.tool_calls)
            if outcome != "completed":
                mark_failed(root, "crew_partial", "partial")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        history.line(f"The crew could not run: {getattr(exc, 'message', None) or type(exc).__name__}", "error")
        await history.close("failed", type(exc).__name__)
        raise
    finally:
        _release()
    if stopped:
        history.line(f"Crew stopped early: {stopped}", "error")
        await history.close("failed", stopped)  # the agent that was running when it stopped
    await history.start("validate", "Validate the report")
    report, decided, mode = _report(inv, crew_run, raw_text, stopped)
    _log_validation(history, report, decided, crew_run)
    await history.close("succeeded")
    block = {"version": state["version"], "status": outcome, "stopped": stopped, "agents": crew_run.progress, "tool_calls": crew_run.tool_calls,
             "limits": {**state["limits"], "timeout_seconds": limits["timeout_seconds"], "max_tool_calls": crew_run.tool_limit, "max_iterations_per_agent": limits["max_iterations"]},
             "config": used, "refused": crew_run.refused, "gaps": crew_run.registry.gaps, "validation": decided,
             # Excerpts come from the registry, which was cut from the stored evidence. Model text never lands here.
             "evidence": [crew_run.registry.records[r] for r in cited(report) if r in crew_run.registry.records],
             "registry": [{k: v for k, v in r.items() if k != "excerpt"} for r in crew_run.registry.records.values()]}
    return {"namespace": namespace, "report": report, "mode": mode, "crewai": block}


def _log_validation(history: History, report: Any, decided: dict[str, Any], crew_run: Run) -> None:
    for g in crew_run.registry.gaps:
        history.line(f"Gap: {g['source']}: {g['reason']}", "warning", {"event": "gap", **g})
    if decided.get("fallback"):
        history.line("The reviewer gave no usable JSON report; MOKS wrote its rule-based report over the evidence collected", "warning")
    rejected, removed, lowered, demoted = (decided.get(k) or [] for k in ("citations_rejected", "claims_removed", "confidence_lowered", "ml_only_findings_demoted"))
    history.line(f"Checked by MOKS: {len(rejected)} invented citation(s) rejected{' (' + ', '.join(map(str, rejected)) + ')' if rejected else ''}, "
                 f"{len(removed)} uncited claim(s) removed, {len(lowered)} confidence level(s) lowered"
                 + (f", {len(demoted)} ML-only finding(s) kept as low-confidence causes" if demoted else ""), "warning" if rejected or removed or lowered or demoted else "info",
                 {"event": "validation", **{k: v for k, v in decided.items()}})
    history.line(f"Report: {len(report.observed_facts)} finding(s), {len(report.suspected_causes)} likely cause(s), {len(report.missing_evidence)} missing evidence item(s)",
                 data={"event": "report", "summary": (report.summary or "")[:500]})


def _report(inv: Any, crew_run: Run, raw_text: str, stopped: str | None):
    """The reviewer's answer, validated; or, when there is none, MOKS's usual fallback: the rule-based
    report over what was collected, with the crew's gaps listed."""
    notes = inv.notes
    if stopped:
        notes.append(f"CrewAI investigation incomplete: {stopped}. The report covers the evidence collected before it stopped.")
        crew_run.registry.gap("crew", stopped)
    raw: dict[str, Any] | None = None
    if raw_text:
        try:
            raw = parse_json_object(raw_text)
        except (ValueError, json.JSONDecodeError):
            notes.append("The incident reviewer's answer was not valid JSON; showing the rule-based report.")
    if raw is not None:
        report, decided = validate(raw, crew_run.registry, notes)
        if report.observed_facts or report.suspected_causes or report.summary:
            return report, decided, "crewai"
    fallback = rules_report("", inv.findings, inv._views(), notes)
    fallback.missing_evidence += [f"{g['source']}: {g['reason']}" for g in crew_run.registry.gaps]
    return fallback, {"citations_rejected": [], "claims_removed": [], "confidence_lowered": [], "fallback": "rules"}, "crewai_fallback"
