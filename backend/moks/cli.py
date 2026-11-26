"""moks command line.

  moks migrate                       apply database migrations (advisory-locked; safe from many pods)
  moks db-grants                     create per-component database roles and apply their grants
  moks wait-schema                   wait until the database schema is ready for this image
  moks seed FILE [--upsert]          create environments/connectors/LLM configs from YAML
  moks serve                         run the API (uvicorn)
  moks worker                        run the investigation worker
  moks create-user EMAIL [--password-stdin] [--grant ROLE[:ENV[:NS]] ...]
  moks grant EMAIL ROLE[:ENV[:NS]]   add a role binding
  moks unlink-identity EMAIL         clear an OIDC account's subject, so its next sign-in links it again
  moks verify-audit                  recompute the audit hash chain
  moks executor                      run the action/diagnostics executor (write credentials)
  moks terminal                      run the terminal gateway (pods in the terminal namespace only)
  moks eval DATASET [--mode rules|llm]  run an evaluation dataset and print scores
  moks reindex                       rebuild the knowledge index for the active embedding model
  moks skills register|bootstrap|release   record, first-install or publish skills; never automatic
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any

MIGRATION_LOCK = 7273111


def _alembic_config():
    from alembic.config import Config

    here = Path(__file__).resolve().parent
    cfg = Config()
    cfg.set_main_option("script_location", str(here / "migrations"))
    return cfg


def migrate() -> None:
    """Run Alembic under a PostgreSQL advisory lock so concurrent pods do not race."""
    import asyncpg
    from alembic import command

    from .config import get_settings

    url = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def locked(fn):
        conn = await asyncpg.connect(url)
        try:
            await conn.execute("SELECT pg_advisory_lock($1)", MIGRATION_LOCK)
            await asyncio.to_thread(fn)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", MIGRATION_LOCK)
            await conn.close()

    asyncio.run(locked(lambda: command.upgrade(_alembic_config(), "head")))
    print("migrations applied")


def db_grants() -> None:
    """Create the per-component roles and apply their grants. Run by the migration owner."""
    import asyncpg

    from . import db_roles
    from .config import get_settings

    url = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def run():
        conn = await asyncpg.connect(url)
        try:
            return await db_roles.apply(conn, await conn.fetchval("SELECT current_database()"))
        finally:
            await conn.close()

    changed = asyncio.run(run())
    print(f"grants applied; passwords set for: {', '.join(changed) or 'none'}")


def wait_schema(timeout: int = 300) -> None:
    """Runtime pods hold no schema rights. They wait until the database is at least what they need.
    A revision this image does not know is newer, which expand/contract migrations allow."""
    import time

    import asyncpg
    from alembic.script import ScriptDirectory

    from .config import get_settings

    script = ScriptDirectory.from_config(_alembic_config())
    known, head = {r.revision for r in script.walk_revisions()}, script.get_current_head()
    order = [r.revision for r in script.walk_revisions()]
    url = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")

    async def current():
        conn = await asyncpg.connect(url)
        try:
            return await conn.fetchval("SELECT version_num FROM alembic_version")
        finally:
            await conn.close()

    deadline = time.monotonic() + timeout
    while True:
        try:
            at = asyncio.run(current())
        except Exception as exc:
            at, reason = None, type(exc).__name__
        else:
            reason = "no version yet"
        if at and (at not in known or order.index(at) <= order.index(head)):
            print(f"schema ready at {at}")
            return
        if time.monotonic() > deadline:
            raise SystemExit(f"the schema did not reach {head} within {timeout}s (at {at or reason})")
        time.sleep(3)


async def _seed(path: str, upsert: bool) -> None:
    import yaml
    from sqlalchemy import select

    from . import audit
    from .db import session_scope
    from .models import Connector, Environment, LLMConfig

    data: dict[str, Any] = yaml.safe_load(Path(path).read_text()) or {}
    async with session_scope() as db:
        for e in data.get("environments", []):
            connectors = e.pop("connectors", [])
            env = (await db.execute(select(Environment).where(Environment.name == e["name"]))).scalar_one_or_none()
            if env is None:
                env = Environment(**e)
                db.add(env)
                await db.flush()
                print(f"environment {env.name}: created")
            elif upsert:
                for k, v in e.items():
                    setattr(env, k, v)
                print(f"environment {env.name}: updated")
            for c in connectors:
                row = (await db.execute(select(Connector).where(Connector.environment_id == env.id, Connector.name == c["name"]))).scalar_one_or_none()
                if row is None:
                    db.add(Connector(environment_id=env.id, **c))
                    print(f"  connector {c['name']}: created")
                elif upsert:
                    for k, v in c.items():
                        setattr(row, k, v)
                    print(f"  connector {c['name']}: updated")
        from .api.environments import sync_automation_binding
        from .models import KnowledgeSource

        for ks in data.get("knowledge_sources", []):
            row = (await db.execute(select(KnowledgeSource).where(KnowledgeSource.name == ks["name"]))).scalar_one_or_none()
            if row is None:
                db.add(KnowledgeSource(**{"shared_globally": ks.get("scope_environment", "*") == "*" and ks.get("scope_namespace", "*") == "*", **ks}))
                print(f"knowledge source {ks['name']}: created (first sync is scheduled by the worker)")
            elif upsert:
                for k, v in ks.items():
                    setattr(row, k, v)

        for e in data.get("environments", []):
            env = (await db.execute(select(Environment).where(Environment.name == e["name"]))).scalar_one()
            await sync_automation_binding(db, env.name, env.auto_investigate or env.detection_enabled)
        for l in data.get("llm", []):
            row = (await db.execute(select(LLMConfig).where(LLMConfig.name == l["name"]))).scalar_one_or_none()
            if row is None:
                # The seed runs on every pod start, so it must never override a choice made in the portal.
                active = (await db.execute(select(LLMConfig).where(LLMConfig.purpose == l.get("purpose", "chat"), LLMConfig.is_active.is_(True)))).scalars().first()
                db.add(LLMConfig(**{**l, "is_active": bool(l.get("is_active")) and active is None}))
                print(f"llm {l['name']}: created")
            elif upsert:
                for k, v in l.items():
                    setattr(row, k, v)
                print(f"llm {l['name']}: updated")
        await audit.record(db, action="config.seed", details={"file": os.path.basename(path), "upsert": upsert})


async def _create_user(email: str, password: str | None, grants: list[str]) -> None:
    from sqlalchemy import select

    from . import audit
    from .auth.routes import hasher
    from .config import get_settings
    from .db import session_scope
    from .models import RoleBinding, User
    from .rbac import parse_grant

    mode = get_settings().auth_mode
    async with session_scope() as db:
        user = (await db.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none()
        if user is None:
            if mode == "oidc":
                raise SystemExit("auth mode is oidc: the user is created at first sign-in; run `moks grant` afterwards")
            user = User(email=email.lower(), display_name=email.split("@")[0], auth_provider=mode)
            db.add(user)
            await db.flush()
            print(f"user {email}: created")
        if password:
            if len(password) < 12:
                raise SystemExit("password must be at least 12 characters")
            user.password_hash = hasher.hash(password)
            print(f"user {email}: password set")
        for g in grants:
            b = parse_grant(g)
            exists = (await db.execute(select(RoleBinding).where(RoleBinding.user_id == user.id, RoleBinding.role == b.role,
                                                                 RoleBinding.environment == b.environment, RoleBinding.namespace == b.namespace))).scalar_one_or_none()
            if not exists:
                db.add(RoleBinding(user_id=user.id, role=b.role, environment=b.environment, namespace=b.namespace, source="cli"))
                print(f"user {email}: granted {b.role} on {b.environment}/{b.namespace}")
        await audit.record(db, action="user.cli", actor_email="cli", resource_type="user", resource_id=str(user.id),
                           details={"email": email, "grants": grants, "password_set": bool(password)})


async def _grant(email: str, grant: str) -> None:
    from sqlalchemy import select

    from .db import session_scope
    from .models import User

    async with session_scope() as db:
        if (await db.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none() is None:
            raise SystemExit(f"no user {email}; with OIDC they must sign in once first")
    await _create_user(email, None, [grant])


async def _unlink_identity(email: str) -> None:
    """An OIDC account belongs to one subject (auth/routes.py). When a person's subject really changed, for example
    after the client was registered again at the provider, this clears it and ends their sessions; the next
    sign-in with the account's email links the new subject."""
    from sqlalchemy import select

    from . import audit
    from .auth.sessions import revoke_all_for_user
    from .db import session_scope
    from .models import User

    async with session_scope() as db:
        user = (await db.execute(select(User).where(User.email == email.lower()))).scalar_one_or_none()
        if user is None or user.auth_provider != "oidc":
            raise SystemExit(f"no OIDC user {email}")
        user.subject = None
        await revoke_all_for_user(db, user.id)
        await audit.record(db, action="user.unlink_identity", actor_email="cli", resource_type="user", resource_id=str(user.id), details={"email": user.email})
    print(f"user {email}: identity unlinked; the next sign-in with this email links it again")


async def _verify() -> None:
    from . import audit
    from .db import session_scope

    async with session_scope() as db:
        result = await audit.verify_chain(db)
    print(result)
    if not result["ok"]:
        raise SystemExit(1)


def serve() -> None:
    import uvicorn

    uvicorn.run("moks.main:create_app", factory=True, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
                proxy_headers=True, forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "*"),
                log_config=None, access_log=False, timeout_graceful_shutdown=20)


def terminal() -> None:
    import uvicorn

    from .config import get_settings

    if get_settings().process_role != "terminal":
        raise SystemExit("moks terminal needs MOKS_PROCESS_ROLE=terminal")
    uvicorn.run("moks.terminal.app:create_app", factory=True, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
                proxy_headers=True, forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "*"), ws_max_size=1_048_576,
                log_config=None, access_log=False, timeout_graceful_shutdown=20)


def worker() -> None:
    import signal

    from .config import get_settings
    from .observability import setup_logging, setup_tracing, start_metrics_server
    from .orchestrator.worker import run_worker

    s = get_settings()
    setup_logging(s.log_level, s.otel_service_name + "-worker")
    setup_tracing(s.otel_service_name + "-worker")
    start_metrics_server(s.metrics_port)

    async def main():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run_worker(stop)

    asyncio.run(main())


def executor() -> None:
    import os
    import signal

    os.environ["MOKS_PROCESS_ROLE"] = "executor"
    from .config import get_settings

    get_settings.cache_clear()
    from .actions.executor import run_executor
    from .observability import setup_logging, setup_tracing, start_metrics_server

    s = get_settings()
    setup_logging(s.log_level, s.otel_service_name + "-executor")
    setup_tracing(s.otel_service_name + "-executor")
    start_metrics_server(s.metrics_port)

    async def main():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run_executor(stop)

    asyncio.run(main())


async def _eval(dataset: str, mode: str) -> None:
    from .evals import runner

    rid = await runner.start(dataset, mode)
    summary = await runner.execute(rid)
    from .db import session_scope
    from .models import EvalRun

    async with session_scope() as db:
        run = await db.get(EvalRun, rid)
        if summary.get("kind") == "crewai":  # the incident scenarios: one row of metrics per case, then the gates
            print(f"{'case':32} score  compl  evid   rca    cite   unsup  seconds  tokens")
            for r in run.results:
                print(f"{r['id'][:31]:32} {r['score']:.2f}   {r['completeness']:.2f}   {r['evidence_quality']:.2f}   {r['root_cause_accuracy']:.2f}   "
                      f"{r['citation_validity']:.2f}   {r['unsupported_conclusions']:>3}   {r['execution_seconds']:>7.1f}  {r['tokens']:>6}")
            for g in run.gates:
                print(f"gate {g['name']:22} {g['state']}" + (f" ({g['value']})" if "value" in g else "") + (f": {'; '.join(g['failures'][:3])}" if g["failures"] else ""))
        else:
            for r in run.results:
                failed = [k for k, v in r["checks"].items() if not v]
                print(f"{r['case']:40} {r['score']:.2f}  {'FAILED: ' + ', '.join(failed) if failed else 'ok'}")
    print(f"score {summary['score']:.3f} over {summary.get('cases', len(run.results or []))} cases (run {rid})"
          + ("" if summary.get("gates_passed") is None else f"; gates {'passed' if summary['gates_passed'] else 'FAILED'}"))


async def _skills(a) -> None:
    from .skills import registry

    if a.skills_cmd == "register":
        r = await registry.register_all()
        print({k: r[k] for k in ("registered", "waiting", "rejected")})
    elif a.skills_cmd == "bootstrap":
        print(await registry.bootstrap())
    else:
        print(await registry.release(None, a.reason, names=a.only, remove=a.remove))


async def _reindex() -> None:
    from .db import session_scope
    from .rag import service

    async with session_scope() as db:
        print(await service.reindex(db))


async def _backup_run(dump: str, manifest_path: str | None = None) -> None:
    from datetime import datetime, timezone

    from sqlalchemy import text

    from . import backup
    from .db import session_scope
    from .models import BackupRun

    started = datetime.now(timezone.utc)
    env = os.environ
    result: dict[str, Any] = {}
    counts: dict[str, int] = {}
    size = None
    error = None
    try:
        bucket = env.get("MOKS_BACKUP_BUCKET")
        if not bucket:
            raise backup.BackupError("MOKS_BACKUP_BUCKET is not set")
        key = backup.load_key()
        if Path(dump + ".failed").exists():
            raise backup.BackupError("pg_dump failed")
        async with session_scope() as db:
            size = (await db.execute(text("SELECT pg_database_size(current_database())"))).scalar_one()
            if not manifest_path:  # read after the dump, so only good enough for manual runs
                counts = await backup.table_counts(db)
                version = (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        snapshot: dict[str, Any] = {"tables": counts, "migration": version, "counts_source": "live"} if not manifest_path else {**json.loads(Path(manifest_path).read_text()), "counts_source": "snapshot"}
        counts = snapshot["tables"]
        retention = backup.Retention(int(env.get("MOKS_BACKUP_KEEP_HOURLY", 48)), int(env.get("MOKS_BACKUP_KEEP_DAILY", 30)), int(env.get("MOKS_BACKUP_KEEP_MONTHLY", 12)))
        store = backup.Store(bucket, env.get("MOKS_BACKUP_ENDPOINT"), env.get("MOKS_BACKUP_REGION"))
        result = backup.publish(Path(dump), store, env.get("MOKS_BACKUP_PREFIX", "moks"), key, {**snapshot, "database_bytes": size}, started, retention)
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:500]}"
    async with session_scope() as db:
        db.add(BackupRun(started_at=started, finished_at=datetime.now(timezone.utc), status="failed" if error else "success", object_key=result.get("key"),
                         size_bytes=result.get("bytes"), sha256=result.get("sha256"), database_bytes=size, tables=counts or None, error=error))
    if error:
        print(f"backup failed: {error}", file=sys.stderr)
        raise SystemExit(1)
    print(f"backup ok: {result['key']} ({result['bytes']} bytes), {len(counts)} tables, {result['pruned']} old copies pruned")


async def _table_counts_at(url: str) -> dict[str, int]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from . import backup

    engine = create_async_engine(url)
    try:
        async with async_sessionmaker(engine)() as db:
            return await backup.table_counts(db)
    finally:
        await engine.dispose()


async def _backup_compare() -> None:
    """URLs come from the environment so passwords never appear in a process list."""
    from . import backup

    drift = backup.live_drift(await _table_counts_at(os.environ["MOKS_COMPARE_SOURCE_URL"]), await _table_counts_at(os.environ["MOKS_COMPARE_TARGET_URL"]))
    print(json.dumps({"note": "drift from a live source is expected; `moks backup drill` proves integrity", "drift": drift}, indent=1))


async def _backup_drill(key: str | None) -> None:
    """URLs come from the environment. MOKS_DRILL_ADMIN_URL needs CREATE DATABASE on a server the
    application is not using, or at least a maintenance database that is not the live one."""
    import tempfile

    from . import backup
    from .config import get_settings

    env = os.environ
    store = backup.Store(env["MOKS_BACKUP_BUCKET"], env.get("MOKS_BACKUP_ENDPOINT"), env.get("MOKS_BACKUP_REGION"))
    if key is None:
        prefix = env.get("MOKS_BACKUP_PREFIX", "moks").strip("/")
        found = sorted(k for k in store.keys(f"{prefix}/hourly/") if k.endswith(".dump.enc"))
        if not found:
            raise SystemExit("no backups found")
        key = found[-1]
    live = get_settings().database_url.rsplit("/", 1)[-1].split("?")[0]
    with tempfile.TemporaryDirectory() as work:
        report = await backup.restore_drill(store, key, backup.load_key(), env["MOKS_DRILL_ADMIN_URL"], live, Path(work))
    print(json.dumps(report, indent=1, default=str))
    if not report["ok"]:
        raise SystemExit(1)


def _backup_fetch(key: str | None, out: str) -> None:
    from . import backup

    env = os.environ
    store = backup.Store(env["MOKS_BACKUP_BUCKET"], env.get("MOKS_BACKUP_ENDPOINT"), env.get("MOKS_BACKUP_REGION"))
    if key is None:
        prefix = env.get("MOKS_BACKUP_PREFIX", "moks").strip("/")
        found = sorted(k for k in store.keys(f"{prefix}/hourly/") if k.endswith(".dump.enc"))
        if not found:
            raise SystemExit("no backups found")
        key = found[-1]
    sealed = Path(out + ".enc")
    store.download(key, sealed)
    with sealed.open("rb") as src, open(out, "wb") as dst:
        backup.decrypt_stream(src, dst, backup.load_key())
    sealed.unlink()
    print(f"{key} -> {out}")


async def _ml(a) -> dict:
    from sqlalchemy import select

    from .config import get_settings
    from .db import session_scope
    from .ml import anomaly, learning
    from .models import Environment, Incident

    if a.ml_cmd == "train":
        if a.nodes and not a.environment:
            raise SystemExit("--nodes needs --environment")
        pairs = [(a.environment, "_node")] if a.nodes else [(a.environment, a.namespace)] if a.environment and a.namespace else [
            tuple(x.strip().split(":", 1)) for x in get_settings().ml_anomaly_namespaces.split(",") if ":" in x]
        if not pairs:
            raise SystemExit("give --environment and --namespace (or --nodes), or set MOKS_ML_ANOMALY_NAMESPACES (env:namespace,...; env:_node for nodes)")
        return {"trained": [await anomaly.train(env, ns) for env, ns in pairs]}
    if a.ml_cmd == "benchmark":
        import asyncio as _asyncio

        from .ml import benchmark

        out = await _asyncio.to_thread(benchmark.run, a.seed)
        for alg in out["algorithms"].values():
            alg.pop("skipped", None) if not alg.get("skipped") else None
        return out
    if a.ml_cmd == "reproduce":
        import uuid as _uuid

        return await anomaly.reproduce(_uuid.UUID(a.model_id))
    if a.ml_cmd == "correlate":
        from .ml import correlate

        return await correlate.run(a.environment)
    if a.ml_cmd == "history":
        from .alerts import automation_principal
        from .ml import history

        async with session_scope() as db:  # as the automation principal: only what it may read
            return await history.summarize(db, await automation_principal(db), environment=a.environment, namespace=a.namespace, service=a.service, days=a.days)
    if a.ml_cmd == "evaluate-similar":
        from .ml import evaluation

        return await evaluation.reviewed_similar()
    if a.ml_cmd == "tick":
        async with session_scope() as db:
            names = (await db.execute(select(Environment.name))).scalars().all()
        return {n: await anomaly.tick(n) for n in names}
    if a.ml_cmd == "index-incidents":
        done = 0
        async with session_scope() as db:
            rows = (await db.execute(select(Incident, Environment.name).join(Environment, Environment.id == Incident.environment_id).where(Incident.postmortem_status == "approved"))).all()
            for inc, env_name in rows:
                await learning.capture_incident(db, inc, env_name, inc.postmortem_approved_by)
                done += 1
        return {"incident_records": done}
    async with session_scope() as db:
        return await learning.purge(db)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="moks")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    sub.add_parser("db-grants")
    sub.add_parser("wait-schema")
    sp = sub.add_parser("seed")
    sp.add_argument("file")
    sp.add_argument("--upsert", action="store_true", help="overwrite existing rows with the file's values")
    sp.add_argument("--if-present", action="store_true", help="do nothing if the file does not exist")
    sub.add_parser("serve")
    sub.add_parser("worker")
    cu = sub.add_parser("create-user")
    cu.add_argument("email")
    cu.add_argument("--password-stdin", action="store_true", help="read the password from stdin (local auth mode)")
    cu.add_argument("--password-prompt", action="store_true")
    cu.add_argument("--grant", action="append", default=[], help="role[:environment[:namespace]]")
    g = sub.add_parser("grant")
    g.add_argument("email")
    g.add_argument("grant")
    ui = sub.add_parser("unlink-identity", help="clear an OIDC account's subject; its next sign-in links it again")
    ui.add_argument("email")
    sub.add_parser("verify-audit")
    sub.add_parser("executor")
    sub.add_parser("terminal")
    ev = sub.add_parser("eval")
    ev.add_argument("dataset")
    ev.add_argument("--mode", choices=["rules", "llm"], default="rules")
    bm = sub.add_parser("benchmark", help="the end-to-end investigation benchmark across engines (docs/UNIFIED_INVESTIGATION.md#benchmark)")
    bm.add_argument("--engines", default="langgraph,crewai,crewai_improved,unified", help="comma-separated: langgraph, crewai, crewai_improved, unified")
    bm.add_argument("--llm", action="store_true", help="use the active chat model (needed by the crews and the unified pipeline's agents)")
    bm.add_argument("--improved-flow", default=None, help="the CrewAI console flow that stands for the improved crew")
    bm.add_argument("--case", action="append", help="run only these case ids")
    bm.add_argument("--json", dest="json_path", default=None)
    bm.add_argument("--markdown", dest="md_path", default=None)
    sub.add_parser("reindex")
    sk = sub.add_parser("skills").add_subparsers(dest="skills_cmd", required=True)
    sk.add_parser("register", help="record the skills in this image; publishes nothing")
    sk.add_parser("bootstrap", help="first install: publish the image's built-ins if no generation exists")
    rel = sk.add_parser("release", help="publish the image's built-ins, or the named skills, as one generation")
    rel.add_argument("--reason", required=True)
    rel.add_argument("--only", nargs="*", help="skill names; default: every built-in in this image")
    rel.add_argument("--remove", nargs="*", default=[], help="skills to drop from the generation, by name")
    bk = sub.add_parser("backup").add_subparsers(dest="backup_cmd", required=True)
    br = bk.add_parser("run", help="encrypt a pg_dump file, upload it, verify the copy and apply retention")
    br.add_argument("--dump", required=True)
    br.add_argument("--manifest", help="the manifest written with the dump, from the same snapshot")
    bf = bk.add_parser("fetch", help="download and decrypt a backup (default: the newest)")
    bf.add_argument("--key")
    bf.add_argument("--out", required=True)
    bk.add_parser("compare", help="report drift between MOKS_COMPARE_SOURCE_URL and MOKS_COMPARE_TARGET_URL")
    bd = bk.add_parser("drill", help="restore a backup into a new isolated database, prove it, drop it")
    bd.add_argument("--key")
    ml = sub.add_parser("ml", help="ML capabilities (docs/ML.md)").add_subparsers(dest="ml_cmd", required=True)
    mt = ml.add_parser("train", help="offline anomaly training for one namespace, or every namespace in MOKS_ML_ANOMALY_NAMESPACES; registers candidates, promotes nothing")
    mt.add_argument("--environment")
    mt.add_argument("--namespace")
    mt.add_argument("--nodes", action="store_true", help="train node models (the infrastructure level) for --environment")
    ml.add_parser("tick", help="score every active anomaly model once and regroup the anomalies (shadow output only)")
    mb = ml.add_parser("benchmark", help="every detector and the correlation engine on synthetic incidents with known answers (no cluster, no database writes)")
    mb.add_argument("--seed", type=int, default=7)
    mr = ml.add_parser("reproduce", help="refit a model from its frozen training snapshot and compare the state")
    mr.add_argument("model_id")
    mc = ml.add_parser("correlate", help="group one environment's recent anomalies now")
    mc.add_argument("--environment", required=True)
    mh = ml.add_parser("history", help="recurring failures, infrastructure problems, runbooks and what worked, as the automation principal may read them")
    mh.add_argument("--environment")
    mh.add_argument("--namespace")
    mh.add_argument("--service")
    mh.add_argument("--days", type=int, default=180)
    ml.add_parser("index-incidents", help="write learning records for postmortems approved before learning records existed")
    ml.add_parser("purge", help="apply learning-record and dataset retention")
    ml.add_parser("evaluate-similar", help="similar-incident retrieval measured on reviewed operator judgments (read-only)")
    from .rag import shared_ingest

    shared_ingest.add_arguments(sub.add_parser("shared-kb", help="the shared SRE knowledge base, moks-rag (docs/SHARED_KNOWLEDGE.md): ingest, delete, sync, status"))
    a = p.parse_args(argv)

    if a.cmd == "migrate":
        migrate()
    elif a.cmd == "db-grants":
        db_grants()
    elif a.cmd == "wait-schema":
        wait_schema()
    elif a.cmd == "seed":
        if a.if_present and not Path(a.file).exists():
            print(f"{a.file} not present; nothing to seed")
            return
        asyncio.run(_seed(a.file, a.upsert))
    elif a.cmd == "terminal":
        terminal()
    elif a.cmd == "serve":
        serve()
    elif a.cmd == "worker":
        worker()
    elif a.cmd == "create-user":
        pw = None
        if a.password_stdin:
            pw = sys.stdin.readline().rstrip("\n")
        elif a.password_prompt:
            pw = getpass.getpass("Password: ")
        asyncio.run(_create_user(a.email, pw, a.grant))
    elif a.cmd == "grant":
        asyncio.run(_grant(a.email, a.grant))
    elif a.cmd == "unlink-identity":
        asyncio.run(_unlink_identity(a.email))
    elif a.cmd == "verify-audit":
        asyncio.run(_verify())
    elif a.cmd == "executor":
        executor()
    elif a.cmd == "eval":
        asyncio.run(_eval(a.dataset, a.mode))
    elif a.cmd == "benchmark":
        from .intelligence import benchmark

        out = asyncio.run(benchmark.run(engines=tuple(e.strip() for e in a.engines.split(",") if e.strip()), use_llm=a.llm, improved_flow=a.improved_flow, cases=a.case))
        benchmark.write(out, a.json_path, a.md_path)
        print(benchmark.markdown(out))
    elif a.cmd == "reindex":
        asyncio.run(_reindex())
    elif a.cmd == "shared-kb":
        sys.exit(asyncio.run(shared_ingest.run(a)))
    elif a.cmd == "skills":
        asyncio.run(_skills(a))
    elif a.cmd == "ml":
        print(json.dumps(asyncio.run(_ml(a)), indent=1, default=str))
    elif a.cmd == "backup":
        if a.backup_cmd == "run":
            asyncio.run(_backup_run(a.dump, a.manifest))
        elif a.backup_cmd == "drill":
            asyncio.run(_backup_drill(a.key))
        elif a.backup_cmd == "fetch":
            _backup_fetch(a.key, a.out)
        else:
            asyncio.run(_backup_compare())


if __name__ == "__main__":
    main()
