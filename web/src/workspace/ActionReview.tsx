import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { download, workspaceApi, type ActionProposal } from "./api";

export function ActionReview() {
  const cache = useQueryClient();
  const proposals = useQuery({
    queryKey: ["proposals"],
    queryFn: workspaceApi.proposals,
  });
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: workspaceApi.jobs });
  const [job, setJob] = useState("");
  const [action, setAction] = useState(
    '{"type":"reschedule","reason":"Review experiment evidence"}',
  );
  const propose = useMutation({
    mutationFn: () => workspaceApi.propose(job, JSON.parse(action)),
    onSuccess: () => {
      void cache.invalidateQueries({ queryKey: ["proposals"] });
    },
  });
  return (
    <>
      <div className="ws-page-heading">
        <div>
          <span className="ws-eyebrow">
            FROM EVIDENCE TO A REVIEWABLE CHANGE
          </span>
          <h1>Review the decision before acting.</h1>
          <p>
            Proposals retain their experiment and exact approved content. This
            workspace delivers dry runs only.
          </p>
        </div>
      </div>
      <div className="ws-two-column">
        <section className="ws-card">
          <h2>Draft an action</h2>
          <p>
            Your agent can also submit proposals through the published tool
            contract.
          </p>
          <label className="ws-field">
            Evidence experiment
            <select
              value={job}
              onChange={(event) => setJob(event.target.value)}
            >
              <option value="">Choose completed experiment</option>
              {jobs.data
                ?.filter((row) => row.status === "completed")
                .map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.id.slice(0, 8)} · {row.reps} replications
                  </option>
                ))}
            </select>
          </label>
          <label className="ws-field">
            Proposed action JSON
            <textarea
              rows={7}
              value={action}
              onChange={(event) => setAction(event.target.value)}
            />
          </label>
          <button
            className="ws-button primary"
            disabled={!job || propose.isPending}
            onClick={() => propose.mutate()}
          >
            Create proposal
          </button>
          {propose.error && (
            <p role="alert" className="ws-error">
              {propose.error.message}
            </p>
          )}
        </section>
        <aside className="ws-stack">
          {proposals.error && <p role="alert">{proposals.error.message}</p>}
          {proposals.data?.map((proposal) => (
            <ProposalCard key={proposal.id} proposal={proposal} />
          ))}
        </aside>
      </div>
    </>
  );
}

function ProposalCard({ proposal }: { proposal: ActionProposal }) {
  const [reviewer, setReviewer] = useState("");
  const [revision, setRevision] = useState(
    proposal.proposal.expected_operational_revision,
  );
  const [key] = useState(() => crypto.randomUUID());
  const approval = useMutation({
    mutationFn: () =>
      workspaceApi.approve(proposal.id, proposal.proposal.digest, reviewer),
  });
  const delivery = useMutation({
    mutationFn: () =>
      workspaceApi.deliver(
        proposal.id,
        approval.data!.approval_id,
        revision,
        key,
      ),
  });
  return (
    <section className="ws-card">
      <div className="ws-card-heading">
        <h2>Proposal {proposal.id.slice(0, 8)}</h2>
        <span className="ws-badge amber">Dry run only</span>
      </div>
      <p>Evidence experiment {proposal.proposal.result_id.slice(0, 8)}</p>
      <pre className="ws-source">
        {JSON.stringify(JSON.parse(proposal.proposal.action_json), null, 2)}
      </pre>
      <details>
        <summary>Exact proposal identity</summary>
        <code className="ws-source">{proposal.proposal.digest}</code>
      </details>
      {!approval.data ? (
        <>
          <label className="ws-field">
            Reviewer label
            <input
              value={reviewer}
              onChange={(event) => setReviewer(event.target.value)}
              placeholder="Name for the review record"
            />
          </label>
          <button
            className="ws-button"
            disabled={!reviewer.trim() || approval.isPending}
            onClick={() => approval.mutate()}
          >
            Approve this dry run
          </button>
        </>
      ) : (
        <>
          <p>
            Approved for 15 minutes. Only this exact proposal may be delivered.
          </p>
          <label className="ws-field">
            Current revision for dry-run check
            <input
              value={revision}
              onChange={(event) => setRevision(event.target.value)}
            />
          </label>
          <button
            className="ws-button primary"
            disabled={delivery.isPending || !!delivery.data}
            onClick={() => delivery.mutate()}
          >
            Deliver dry run
          </button>
        </>
      )}
      {(approval.error || delivery.error) && (
        <p role="alert" className="ws-error">
          {approval.error?.message || delivery.error?.message}
        </p>
      )}
      {delivery.data && (
        <>
          <p role="status">
            Dry run recorded. No operational system was changed.
          </p>
          <pre className="ws-source">
            {JSON.stringify(delivery.data, null, 2)}
          </pre>
          <button
            className="ws-button"
            onClick={() =>
              download(
                "dry-run-receipt.json",
                JSON.stringify(delivery.data, null, 2),
              )
            }
          >
            Export receipt
          </button>
        </>
      )}
    </section>
  );
}
