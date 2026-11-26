"""Documents for the shared SRE knowledge base (moks-rag): runbooks, architecture docs, manifests and postmortems
(docs/SHARED_KNOWLEDGE.md#ingestion).

Three ways in:
- `moks shared-kb ingest | delete | sync | postmortems | status`, from a checkout, a laptop, CI or a MOKS pod; only
  RAG_API_URL and RAG_BEARER_TOKEN are needed (RAG_COLLECTION optional), except for postmortems, which read the database;
- .github/workflows/shared-knowledge.yml runs `sync` on a push to master with the files it changed and deleted, filtered
  by deploy/shared-knowledge.yaml;
- approving a postmortem queues a shared_kb_push job (jobs.py) that sends it with service, severity, incident_id and
  date, and is retried while moks-rag is down.

Every document has a stable doc_id (repository and path, or the incident id), so sending it again replaces it, and a
file deleted from Git or marked deprecated in its front matter is deleted from the collection by the same id
(DELETE /documents/{doc_id}?collection=...).

Nothing secret is sent. A Kubernetes Secret manifest, anything under a secrets/ directory, .env files and keys are
refused, and every text goes through MOKS's redaction before it leaves.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from ..redaction import redact
from . import shared

DOC_TYPES = ("runbook", "architecture", "manifest", "postmortem")
CONFIG = "deploy/shared-knowledge.yaml"
MAX_FILE_BYTES = 400_000
FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
HEADING = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.M)
REFUSED = [re.compile(p) for p in (r"(^|/)secrets?/", r"(^|/)\.env(\.|$)", r"\.(pem|key|p12|pfx|jks|kubeconfig)$", r"(^|/)kubeconfig[^/]*$", r"(^|/)id_(rsa|ed25519)")]
META_FIELDS = ("service", "severity", "incident_id", "date", "environment", "namespace", "status", "owner")


class Refused(ValueError):
    """A file that must not leave the repository."""


@dataclass
class Source:
    doc_type: str
    include: list[str]
    exclude: list[str] = field(default_factory=list)
    service: str | None = None


@dataclass
class Config:
    repository: str
    sources: list[Source]
    exclude: list[str] = field(default_factory=list)
    service: str | None = None
    branch: str = "master"

    def source_for(self, path: str) -> Source | None:
        if any(glob_match(path, g) for g in self.exclude):
            return None
        return next((s for s in self.sources if any(glob_match(path, g) for g in s.include) and not any(glob_match(path, g) for g in s.exclude)), None)


def glob_match(path: str, pattern: str) -> bool:
    """Git-style globs: ** crosses directories, * and ? do not."""
    rx, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            rx, i = rx + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            rx, i = rx + ".*", i + 2
        elif pattern[i] == "*":
            rx, i = rx + "[^/]*", i + 1
        elif pattern[i] == "?":
            rx, i = rx + "[^/]", i + 1
        else:
            rx, i = rx + re.escape(pattern[i]), i + 1
    return re.fullmatch(rx, str(PurePosixPath(path))) is not None


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text()) or {}
    sources = []
    for s in raw.get("sources") or []:
        if s.get("doc_type") not in DOC_TYPES:
            raise ValueError(f"{path}: doc_type must be one of {', '.join(DOC_TYPES)}, not {s.get('doc_type')!r}")
        sources.append(Source(s["doc_type"], list(s.get("include") or []), list(s.get("exclude") or []), s.get("service")))
    if not raw.get("repository"):
        raise ValueError(f"{path}: repository is required (owner/name), it is part of every doc_id")
    return Config(str(raw["repository"]), sources, list(raw.get("exclude") or []), raw.get("service"), str(raw.get("branch") or "master"))


# ---- documents ------------------------------------------------------------------------------------------------


def _scalar(v: Any) -> Any:
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v if isinstance(v, (str, int, float, bool)) else str(v)


def front_matter(text: str) -> tuple[dict[str, Any], str]:
    m = FRONT_MATTER.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}, text
    return (meta if isinstance(meta, dict) else {}), text[m.end():]


def deprecated(meta: dict[str, Any]) -> bool:
    return meta.get("deprecated") is True or str(meta.get("status") or "").lower() in ("deprecated", "retired", "archived")


def _manifest_meta(text: str, path: str) -> dict[str, Any]:
    """Kinds, names and the namespace of a plain manifest. A template that is not YAML (Helm) is sent as text."""
    try:
        docs = [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    except yaml.YAMLError:
        return {}
    if any(str(d.get("kind")) == "Secret" for d in docs):
        raise Refused(f"{path} holds a Kubernetes Secret")
    names = [f"{d.get('kind')}/{(d.get('metadata') or {}).get('name')}" for d in docs if d.get("kind") and isinstance(d.get("metadata"), dict)]
    spaces = {str((d.get("metadata") or {}).get("namespace")) for d in docs if isinstance(d.get("metadata"), dict) and (d.get("metadata") or {}).get("namespace")}
    out: dict[str, Any] = {}
    if names:
        out["resources"] = ", ".join(names)[:500]
    if len(spaces) == 1 and "{{" not in next(iter(spaces)):
        out["namespace"] = next(iter(spaces))
    return out


def file_doc(path: str, text: str, doc_type: str, *, repository: str, revision: str | None = None, branch: str = "master",
             service: str | None = None, extra: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, bool]:
    """(the ingest body, deprecated). The body is None when the file is deprecated: its doc_id is to be deleted."""
    if any(rx.search(path) for rx in REFUSED):
        raise Refused(f"{path} looks like a secret or credential file")
    if doc_type not in DOC_TYPES:
        raise ValueError(f"doc_type must be one of {', '.join(DOC_TYPES)}")
    meta, body = front_matter(text) if path.endswith((".md", ".markdown", ".mdx")) else ({}, text)
    doc_id = shared.doc_id_for(repository.split("/")[-1], path)
    if deprecated(meta):
        return None, True
    metadata: dict[str, Any] = {"doc_type": str(meta.get("doc_type") or doc_type), "repository": repository, "path": path}
    if doc_type == "manifest":
        metadata.update(_manifest_meta(text, path))
    if revision:
        metadata["revision"] = revision
    metadata["url"] = f"https://github.com/{repository}/blob/{revision or branch}/{path}"
    if service:
        metadata["service"] = service
    for k in META_FIELDS:
        if meta.get(k) not in (None, ""):
            metadata[k] = _scalar(meta[k])
    metadata.update({k: _scalar(v) for k, v in (extra or {}).items() if v not in (None, "")})
    heading = HEADING.search(body)
    title = str(meta.get("title") or (heading.group(1) if heading else "") or PurePosixPath(path).name)[:300]
    clean = redact(body).strip()
    if not clean:
        raise ValueError(f"{path} is empty")
    return {"doc_id": doc_id, "title": title, "source": metadata["url"], "text": clean, "metadata": metadata}, False


def _service_of(resource: str | None) -> str | None:
    kind, _, name = str(resource or "").partition("/")
    return name or None if kind else None


def postmortem_doc(inc: Any, env_name: str, markdown: str, public_url: str) -> dict[str, Any]:
    """An approved MOKS postmortem, with the metadata triage filters and ranks on."""
    url = f"{public_url.rstrip('/')}/incidents/{inc.id}"
    when = inc.resolved_at or inc.created_at
    pm = inc.postmortem or {}
    metadata = {"doc_type": "postmortem", "incident_id": str(inc.id), "service": _service_of(inc.resource) or inc.owner_team, "severity": inc.severity,
                "date": when.date().isoformat() if when else None, "environment": env_name, "namespace": inc.namespace, "url": url,
                "root_cause_verified": pm.get("root_cause_verified") if isinstance(pm.get("root_cause_verified"), bool) else None}
    return {"doc_id": shared.doc_id_for("moks-postmortem", str(inc.id)), "title": f"Postmortem: {inc.title}"[:300], "source": url, "text": redact(markdown),
            "metadata": {k: v for k, v in metadata.items() if v not in (None, "")}}


async def push_postmortem(db: Any, incident_id: uuid.UUID, client: shared.SharedRag | None = None) -> dict[str, Any]:
    """The shared_kb_push job: one approved postmortem into moks-rag. A failure raises, so the job is retried."""
    from ..config import get_settings
    from ..incidents import add_event, incident_detail, report_markdown
    from ..models import Environment, Incident

    client = client or shared.active()
    if client is None:
        return {"skipped": shared.why_off()}
    inc = await db.get(Incident, incident_id)
    if inc is None or inc.postmortem_status != "approved":
        return {"skipped": "the incident has no approved postmortem"}
    env = await db.get(Environment, inc.environment_id)
    doc = postmortem_doc(inc, env.name, report_markdown(await incident_detail(db, inc)), get_settings().public_url)
    try:
        r = await client.ingest(doc)
    except shared.SharedRagError as exc:
        raise PushFailed(exc.kind, exc.message) from None
    await add_event(db, inc.id, "note", {"text": f"Postmortem added to the shared SRE knowledge base (moks-rag {client.collection})", "doc_id": doc["doc_id"]})
    return {"doc_id": doc["doc_id"], "chunks": r.get("chunks_ingested")}


class PushFailed(Exception):
    """A job failure with a kind, so the worker logs it as expected (no stack) and retries it."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind, self.message = kind, message


# ---- sync -------------------------------------------------------------------------------------------------------


@dataclass
class Outcome:
    path: str
    doc_id: str
    action: str  # ingested | deleted | refused | failed | would_ingest | would_delete
    detail: str = ""
    chunks: int | None = None


async def sync(client: shared.SharedRag, cfg: Config, root: Path, *, changed: list[str] = (), deleted: list[str] = (), revision: str | None = None,
               dry_run: bool = False) -> list[Outcome]:
    """Ingest each changed file a source covers and delete each deleted one (and each changed one now deprecated)."""
    out: list[Outcome] = []
    repo = cfg.repository.split("/")[-1]
    for path in dict.fromkeys(p.strip() for p in deleted if p.strip()):
        if cfg.source_for(path) is None:
            continue
        out.append(await _delete(client, path, shared.doc_id_for(repo, path), dry_run))
    for path in dict.fromkeys(p.strip() for p in changed if p.strip()):
        src = cfg.source_for(path)
        if src is None:
            continue
        doc_id = shared.doc_id_for(repo, path)
        file = root / path
        try:
            if not file.is_file():
                out.append(await _delete(client, path, doc_id, dry_run))  # gone since the diff was taken
                continue
            if file.stat().st_size > MAX_FILE_BYTES:
                out.append(Outcome(path, doc_id, "refused", f"larger than {MAX_FILE_BYTES} bytes"))
                continue
            body, gone = file_doc(path, file.read_text(encoding="utf-8"), src.doc_type, repository=cfg.repository, revision=revision, branch=cfg.branch,
                                  service=src.service or cfg.service)
        except Refused as exc:
            out.append(Outcome(path, doc_id, "refused", str(exc)))
            continue
        except (ValueError, UnicodeDecodeError) as exc:
            out.append(Outcome(path, doc_id, "failed", str(exc)[:300]))
            continue
        if gone:
            out.append(await _delete(client, path, doc_id, dry_run, "deprecated in its front matter"))
            continue
        if dry_run:
            out.append(Outcome(path, doc_id, "would_ingest", body["metadata"]["doc_type"]))
            continue
        try:
            r = await client.ingest(body)
            out.append(Outcome(path, doc_id, "ingested", body["metadata"]["doc_type"], r.get("chunks_ingested")))
        except shared.SharedRagError as exc:
            out.append(Outcome(path, doc_id, "failed", str(exc)))
    return out


async def _delete(client: shared.SharedRag, path: str, doc_id: str, dry_run: bool, why: str = "deleted") -> Outcome:
    if dry_run:
        return Outcome(path, doc_id, "would_delete", why)
    try:
        await client.delete(doc_id)
        return Outcome(path, doc_id, "deleted", why)
    except shared.SharedRagError as exc:
        return Outcome(path, doc_id, "failed", str(exc))


def every_file(cfg: Config, root: Path) -> list[str]:
    """Every file under root a source covers, for a first full load."""
    return sorted(p for p in (str(f.relative_to(root).as_posix()) for f in root.rglob("*") if f.is_file() and ".git" not in f.parts) if cfg.source_for(p))


# ---- command line -------------------------------------------------------------------------------------------------


def add_arguments(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="shared_cmd", required=True)
    sub.add_parser("status", help="whether moks-rag is configured and ready (RAG_API_URL, RAG_BEARER_TOKEN, RAG_COLLECTION)")
    ing = sub.add_parser("ingest", help="send files as documents, whatever deploy/shared-knowledge.yaml says")
    ing.add_argument("paths", nargs="+")
    ing.add_argument("--doc-type", choices=DOC_TYPES, required=True)
    ing.add_argument("--repository", required=True, help="owner/name: part of each doc_id and of its GitHub link")
    ing.add_argument("--root", default=".", help="the checkout the paths are relative to")
    ing.add_argument("--revision")
    for f in META_FIELDS:
        ing.add_argument(f"--{f.replace('_', '-')}", dest=f)
    dl = sub.add_parser("delete", help="delete documents by doc_id (a deprecated runbook)")
    dl.add_argument("doc_ids", nargs="+")
    sy = sub.add_parser("sync", help="ingest changed and delete deleted files that deploy/shared-knowledge.yaml covers")
    sy.add_argument("--config", default=CONFIG)
    sy.add_argument("--root", default=".")
    sy.add_argument("--changed", nargs="*", default=[], help="paths added or modified (git diff --name-status A/M, and R's new path)")
    sy.add_argument("--deleted", nargs="*", default=[], help="paths deleted (D, and R's old path)")
    sy.add_argument("--all", action="store_true", help="every file the sources cover: the first load")
    sy.add_argument("--revision")
    sy.add_argument("--dry-run", action="store_true")
    sub.add_parser("postmortems", help="send every approved MOKS postmortem (reads the MOKS database)")


async def run(a: argparse.Namespace, client: shared.SharedRag | None = None) -> int:
    """The exit status: 0 when everything asked for was done."""
    client = client or shared.from_env()
    if a.shared_cmd == "status":
        print(json.dumps({"url": client.where(), "collection": client.collection, **(await client.ready())}))
        return 0
    if not client.configured:
        print(f"moks-rag is not configured: set {shared.ENV_URL} and {shared.ENV_TOKEN}", file=sys.stderr)
        return 2
    if a.shared_cmd == "delete":
        results = [await _delete(client, "", d, False) for d in a.doc_ids]
    elif a.shared_cmd == "ingest":
        root = Path(a.root)
        results = []
        extra = {f: getattr(a, f) for f in META_FIELDS if getattr(a, f, None)}
        for path in a.paths:
            rel = Path(path).resolve().relative_to(root.resolve()).as_posix() if Path(path).is_absolute() else path
            doc_id = shared.doc_id_for(a.repository.split("/")[-1], rel)
            try:
                body, gone = file_doc(rel, (root / rel).read_text(encoding="utf-8"), a.doc_type, repository=a.repository, revision=a.revision, extra=extra)
            except (Refused, ValueError, OSError, UnicodeDecodeError) as exc:
                results.append(Outcome(rel, doc_id, "refused" if isinstance(exc, Refused) else "failed", str(exc)[:300]))
                continue
            if gone:
                results.append(await _delete(client, rel, doc_id, False, "deprecated in its front matter"))
                continue
            try:
                r = await client.ingest(body)
                results.append(Outcome(rel, doc_id, "ingested", a.doc_type, r.get("chunks_ingested")))
            except shared.SharedRagError as exc:
                results.append(Outcome(rel, doc_id, "failed", str(exc)))
    elif a.shared_cmd == "sync":
        cfg, root = load_config(Path(a.root) / a.config if not Path(a.config).is_absolute() else Path(a.config)), Path(a.root)
        changed = every_file(cfg, root) if a.all else a.changed
        results = await sync(client, cfg, root, changed=changed, deleted=a.deleted, revision=a.revision, dry_run=a.dry_run)
    else:  # postmortems
        results = await _all_postmortems(client)
    for r in results:
        print(json.dumps(r.__dict__))
    failed = [r for r in results if r.action == "failed"]
    print(f"{len(results)} document(s): " + ", ".join(f"{sum(1 for r in results if r.action == k)} {k}" for k in sorted({r.action for r in results})) if results else "nothing to do",
          file=sys.stderr)
    return 1 if failed else 0


async def _all_postmortems(client: shared.SharedRag) -> list[Outcome]:
    from sqlalchemy import select

    from ..db import session_scope
    from ..models import Incident

    async with session_scope() as db:
        ids = list((await db.execute(select(Incident.id).where(Incident.postmortem_status == "approved"))).scalars())
    out = []
    for iid in ids:
        doc_id = shared.doc_id_for("moks-postmortem", str(iid))
        try:
            async with session_scope() as db:
                r = await push_postmortem(db, iid, client)
            out.append(Outcome(f"incidents/{iid}", doc_id, "ingested" if "doc_id" in r else "skipped", r.get("skipped", ""), r.get("chunks")))
        except PushFailed as exc:
            out.append(Outcome(f"incidents/{iid}", doc_id, "failed", f"{exc.kind}: {exc.message}"))
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="moks shared-kb", description="The shared SRE knowledge base (moks-rag): docs/SHARED_KNOWLEDGE.md")
    add_arguments(p)
    sys.exit(asyncio.run(run(p.parse_args(argv))))


if __name__ == "__main__":
    main()
