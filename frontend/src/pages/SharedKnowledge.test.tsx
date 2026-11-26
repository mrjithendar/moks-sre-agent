import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SharedKnowledge } from "./SharedKnowledge";

const runbook = (over: any = {}) => ({ ref: "E2", doc_id: "moks-sre-agent:docs:runbooks:payments.md", kind: "runbook", title: "Payments crash loop", rank: 2, score: 0.4,
  url: "https://github.com/mrjithendar/moks-sre-agent/blob/master/docs/runbooks/payments.md", matched: ["payments", "OOMKilled", "exit code 137"],
  steps: ["Read the previous container's logs", "`kubectl -n shop rollout undo deploy/payments`"], excerpt: "When payments is OOMKilled", metadata: {}, ...over });
const incident = (over: any = {}) => ({ ref: "E2", doc_id: "moks-postmortem:42", kind: "postmortem", title: "Postmortem: payments OOM", rank: 1, score: 0.5,
  url: "javascript:alert(1)", matched: [], steps: [], excerpt: "Raised the memory limit", metadata: { date: "2026-10-02", severity: "sev2", service: "payments" }, ...over });
const block = (over: any = {}) => ({ status: "ok", note: "From the shared SRE knowledge base (moks-rag): documentation and history, never the current state of anything.",
  searches: [{ stage: "refresh", ref: "E2", status: "ok", query: "KubePodCrashLooping payments namespace shop | OOMKilled", identifiers: ["payments", "OOMKilled"], results: 2, filtered_out: 1 }],
  runbooks: [runbook()], incidents: [incident()], manifests: [], counts: { passages: 2, runbooks: 1, incidents: 1, manifests: 0 }, ...over });

afterEach(cleanup);

describe("Shared SRE knowledge", () => {
  it("shows each runbook's steps and what it matched, past incidents with their facts, and the query", () => {
    const onCite = vi.fn();
    render(<SharedKnowledge s={block()} onCite={onCite} />);
    expect(screen.getByRole("link", { name: "Payments crash loop" }).getAttribute("href")).toContain("docs/runbooks/payments.md");
    expect(screen.getByText("matches payments, OOMKilled, exit code 137")).toBeTruthy();
    expect(screen.getByText("Read the previous container's logs")).toBeTruthy();
    expect(screen.getByText("2026-10-02 · sev2 · payments")).toBeTruthy();
    expect(screen.queryByRole("link", { name: "Postmortem: payments OOM" })).toBeNull();  // never a link from a non-web address
    expect(screen.getByText("ranked by meaning")).toBeTruthy();
    expect(screen.getByText(/1 left out/)).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "E2" })[0]);
    expect(onCite).toHaveBeenCalledWith("E2");
  });

  it("says moks-rag could not be searched and that triage used live evidence only", () => {
    render(<SharedKnowledge s={block({ status: "failed", runbooks: [], incidents: [], counts: { passages: 0 },
      searches: [{ stage: "initial", ref: "E1", status: "failed", reason: "moks-rag is unreachable at http://rag-api.rag.svc.cluster.local:8000 (ConnectError)" }] })} onCite={vi.fn()} />);
    expect(screen.getByText("unavailable")).toBeTruthy();
    expect(screen.getByText(/rests on the alert, logs and cluster evidence only/)).toBeTruthy();
    expect(screen.getAllByText(/unreachable at http:\/\/rag-api/).length).toBe(2);  // in the warning and beside its search
  });

  it("renders nothing where moks-rag is not configured or the request is not a triage", () => {
    const { container } = render(<><SharedKnowledge s={undefined} onCite={vi.fn()} /><SharedKnowledge s={{ status: "not_attempted", reason: "not configured", searches: [] }} onCite={vi.fn()} /></>);
    expect(container.textContent).toBe("");
  });
});
