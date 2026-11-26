import { Badge } from "../components";
import { Markdown } from "../markdown";

/** The report's shared_knowledge block (backend orchestrator/shared_knowledge.py): what moks-rag returned for this triage. */
const STATUS: Record<string, ["ok" | "info" | "warn" | "bad" | "neutral", string]> = {
  ok: ["ok", "searched"], empty: ["neutral", "nothing matched"], failed: ["bad", "unavailable"], skipped: ["neutral", "not searched"], not_attempted: ["neutral", "not searched"],
};
const safeLink = (url?: string | null) => (url && /^https?:\/\//i.test(url) ? url : undefined);

function Doc({ d, onCite }: { d: any; onCite: (r: string) => void }) {
  const href = safeLink(d.url);
  const m = d.metadata ?? {};
  const facts = [m.date, m.severity, m.service].filter(Boolean);
  const title = d.title || d.doc_id;
  return <li className="passage">
    <div className="row tight wrap">
      {d.ref && <button type="button" className="cite" style={{ marginLeft: 0 }} onClick={() => onCite(d.ref)}>{d.ref}</button>}
      {href ? <a href={href} target="_blank" rel="noreferrer noopener"><strong>{title}</strong></a> : <strong>{title}</strong>}
      {d.matched?.length > 0 ? <Badge kind="info">matches {d.matched.slice(0, 4).join(", ")}</Badge> : <Badge kind="neutral">ranked by meaning</Badge>}
      {facts.length > 0 && <span className="small muted">{facts.join(" · ")}</span>}
    </div>
    {d.steps?.length > 0
      ? <ol className="small">{d.steps.map((s: string, i: number) => <li key={i}><Markdown text={s} /></li>)}</ol>
      : d.excerpt && <div className="small muted excerpt">{d.excerpt}</div>}
    <div className="small muted mono">{d.doc_id}</div>
  </li>;
}

export function SharedKnowledge({ s, onCite }: { s?: any; onCite: (r: string) => void }) {
  if (!s || (s.status === "not_attempted" && !s.searches?.length)) return null;  // not configured here, or not a triage
  const [tone, label] = STATUS[s.status] ?? ["neutral", s.status];
  const c = s.counts ?? {};
  const runbooks: any[] = s.runbooks ?? [], incidents: any[] = s.incidents ?? [], manifests: any[] = s.manifests ?? [], searches: any[] = s.searches ?? [];
  const failure = searches.find((q) => q.status === "failed")?.reason;
  return <details className="rs knowledge shared-knowledge" open={s.status === "failed" || runbooks.length + incidents.length > 0}>
    <summary>Shared SRE knowledge<span className="count">{c.passages ?? 0}</span> <Badge kind={tone}>{label}</Badge></summary>
    <div className="small muted">{s.note}</div>
    {s.status === "failed" && <div className="alert warn small">moks-rag could not be searched{failure ? ` (${failure})` : ""}. This triage rests on the alert, logs and cluster evidence only.</div>}
    {s.status === "empty" && <div className="alert info small">moks-rag had no runbook, postmortem or manifest for this alert that you may read.</div>}
    {runbooks.length > 0 && <><div className="small"><strong>Runbooks</strong></div><ul className="passages">{runbooks.map((d) => <Doc key={`${d.ref}-${d.doc_id}-${d.rank}`} d={d} onCite={onCite} />)}</ul></>}
    {incidents.length > 0 && <><div className="small"><strong>Past incidents</strong></div><ul className="passages">{incidents.map((d) => <Doc key={`${d.ref}-${d.doc_id}-${d.rank}`} d={d} onCite={onCite} />)}</ul></>}
    {manifests.length > 0 && <details className="small"><summary>Manifests ({manifests.length})</summary><ul className="passages">{manifests.map((d) => <Doc key={`${d.ref}-${d.doc_id}-${d.rank}`} d={d} onCite={onCite} />)}</ul></details>}
    {searches.length > 0 && <details className="small"><summary>Searches ({searches.length})</summary>
      <ul>{searches.map((q, i) => <li key={i}>{q.query && <span className="mono">{q.query}</span>} <Badge kind={q.status === "ok" ? "neutral" : q.status === "failed" ? "bad" : "warn"}>{q.stage} · {q.status === "ok" ? `${q.results} result(s)` : q.status}</Badge>
        {q.identifiers?.length > 0 && <div className="muted">Exact: <span className="mono">{q.identifiers.join(", ")}</span></div>}
        {q.filtered_out > 0 && <div className="muted">{q.filtered_out} left out: another environment, or a namespace outside your scope</div>}
        {q.reused && <span className="muted"> · reused after a restart</span>}{q.reason && <div className="muted">{q.reason}</div>}</li>)}</ul></details>}
  </details>;
}
