import { useState } from "react";
import { DecisionLab } from "./DecisionLab";
import { ActionReview } from "./ActionReview";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { workspaceApi, download, setServiceToken, type Scenario } from "./api";
import { Icon } from "./Icons";
import { ImportDialog } from "./ImportDialog";
import { ScenarioView } from "./ScenarioView";
import { ExperimentView } from "./ExperimentView";
import "./workspace.css";

const navigation = [
  { id: "library", icon: "layers", label: "Scenario library" },
  { id: "experiments", icon: "play", label: "Experiments" },
  { id: "build", icon: "spark", label: "Build a twin" },
  { id: "data", icon: "box", label: "Operational data" },
  { id: "schedule", icon: "clock", label: "Scheduling" },
  { id: "actions", icon: "check", label: "Action review" },
  { id: "agents", icon: "code", label: "Agent access" },
];

export default function WorkspaceApp() {
  const cache = useQueryClient();
  const [page, setPage] = useState(
    new URLSearchParams(location.search).get("page") || "library",
  );
  const [selectedId, setSelectedId] = useState(
    new URLSearchParams(location.search).get("scenario") || "",
  );
  const [dialog, setDialog] = useState<"import" | "branch" | null>(null);
  const [search, setSearch] = useState("");
  const scenarios = useQuery({
    queryKey: ["scenarios"],
    queryFn: workspaceApi.scenarios,
  });
  const examples = useQuery({
    queryKey: ["examples"],
    queryFn: workspaceApi.examples,
  });
  const jobs = useQuery({
    queryKey: ["experiments"],
    queryFn: workspaceApi.jobs,
    refetchInterval: 2000,
  });
  const selected = scenarios.data?.find(
    (scenario) => scenario.id === selectedId,
  );
  function navigate(next: string, id = "") {
    setPage(next);
    setSelectedId(id);
    history.replaceState(
      null,
      "",
      `?page=${next}${id ? `&scenario=${id}` : ""}`,
    );
  }
  function imported(scenario: Scenario) {
    void cache.invalidateQueries({ queryKey: ["scenarios"] });
    setDialog(null);
    navigate("library", scenario.id);
  }
  const load = useMutation({
    mutationFn: workspaceApi.load,
    onSuccess: imported,
  });
  const run = useMutation({
    mutationFn: (reps: number) => workspaceApi.evaluate(selectedId, reps),
    onSuccess: () => {
      void cache.invalidateQueries({ queryKey: ["experiments"] });
      navigate("experiments");
    },
  });
  const cancel = useMutation({
    mutationFn: workspaceApi.cancel,
    onSuccess: () => {
      void cache.invalidateQueries({ queryKey: ["experiments"] });
    },
  });
  const error =
    scenarios.error ||
    examples.error ||
    jobs.error ||
    load.error ||
    run.error ||
    cancel.error;
  const visible = (scenarios.data ?? []).filter((scenario) =>
    scenario.name.toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <div className="ws-shell">
      <aside className="ws-sidebar">
        <a className="ws-brand" href="?page=library">
          <span className="ws-logo">
            <Icon name="layers" size={24} />
          </span>
          twinflow<span className="ws-brand-dot">.</span>
        </a>
        <div className="ws-space-switch">
          <span className="ws-space-avatar">O</span>
          <div>
            <strong>Operations workspace</strong>
            <small>Local · private by default</small>
          </div>
        </div>
        <div className="ws-nav-label">WORKSPACE</div>
        <nav aria-label="Main navigation">
          {navigation.map((item) => (
            <button
              key={item.id}
              className={page === item.id ? "active" : ""}
              onClick={() => navigate(item.id)}
            >
              <Icon name={item.icon} size={19} />
              {item.label}
              {item.id === "experiments" && !!jobs.data?.length && (
                <span className="ws-count">{jobs.data.length}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="ws-sidebar-bottom">
          <div className="ws-agent-callout">
            <Icon name="spark" />
            <strong>Built for your agents.</strong>
            <p>
              Give them a model of your operation. Let them test the
              possibilities.
            </p>
            <button onClick={() => navigate("agents")}>
              Connect an agent
              <Icon name="arrow" size={15} />
            </button>
          </div>
          <a href="?view=legacy" className="ws-legacy-link">
            Classic simulation tools
            <Icon name="arrow" size={14} />
          </a>
          <div className="ws-user">
            <span>TF</span>
            <div>
              <strong>Your workspace</strong>
              <small>Portable operational twins</small>
            </div>
          </div>
        </div>
      </aside>
      <div className="ws-content">
        <header className="ws-topbar">
          <div>
            <span className="ws-status-dot connected" />
            Operational intelligence<span className="ws-topbar-divider">/</span>
            <span>{navigation.find((item) => item.id === page)?.label}</span>
          </div>
          <div className="ws-topbar-actions">
            <a href="/docs" target="_blank" rel="noreferrer">
              API reference
              <Icon name="code" size={15} />
            </a>
            <button
              className="ws-button small"
              onClick={() => setDialog("import")}
            >
              <Icon name="upload" size={15} />
              Import scenario
            </button>
          </div>
        </header>
        <main className="ws-main">
          {error && (
            <div className="ws-error" role="alert">
              {error.message}
              <button
                onClick={() => {
                  void cache.invalidateQueries();
                  load.reset();
                  run.reset();
                  cancel.reset();
                }}
              >
                Retry
              </button>
            </div>
          )}
          {page === "library" && selected && (
            <>
              <button className="ws-back" onClick={() => navigate("library")}>
                ← All scenarios
              </button>
              <ScenarioView
                scenario={selected}
                jobs={jobs.data ?? []}
                onRun={(reps) => run.mutate(reps)}
                onBranch={() => setDialog("branch")}
                busy={run.isPending}
              />
            </>
          )}
          {page === "library" && !selected && (
            <>
              <div className="ws-page-heading">
                <div>
                  <span className="ws-eyebrow">
                    YOUR OPERATIONS, EXPLORABLE
                  </span>
                  <h1>
                    Understand today.
                    <br />
                    <span>Test what comes next.</span>
                  </h1>
                  <p>
                    Load a scenario. Explore a process. Give your agents a place
                    to test decisions.
                  </p>
                </div>
                <button
                  className="ws-button primary"
                  onClick={() => setDialog("import")}
                >
                  <Icon name="plus" size={17} />
                  Import a scenario
                </button>
              </div>
              <div className="ws-library-toolbar">
                <div>
                  <h2>
                    Scenario library{" "}
                    <span className="ws-inline-count">
                      {scenarios.data?.length ?? 0}
                    </span>
                  </h2>
                  <p>Versioned models of your operation, ready to explore.</p>
                </div>
                <input
                  type="search"
                  aria-label="Search scenarios"
                  placeholder="Search scenarios…"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                />
              </div>
              {scenarios.isLoading ? (
                <div
                  className="ws-skeleton-grid"
                  aria-label="Loading scenarios"
                >
                  {[1, 2, 3].map((i) => (
                    <div key={i} className="ws-skeleton" />
                  ))}
                </div>
              ) : visible.length ? (
                <div className="ws-scenario-grid">
                  {visible.map((scenario) => (
                    <button
                      key={scenario.id}
                      className="ws-scenario-card"
                      onClick={() => navigate("library", scenario.id)}
                    >
                      <div className="ws-scenario-art">
                        <div className="ws-mini-route">
                          {["box", "flow", "check"].map((icon, i) => (
                            <span key={icon}>
                              <Icon name={icon} size={20} />
                              {i < 2 && <i />}
                            </span>
                          ))}
                        </div>
                        <span className="ws-badge">
                          {scenario.parent_id ? "Alternative" : "Baseline"}
                        </span>
                      </div>
                      <div className="ws-scenario-body">
                        <h3>{scenario.name}</h3>
                        <p>
                          {scenario.summary.processes} processes <span>·</span>{" "}
                          {scenario.summary.orders} orders <span>·</span>{" "}
                          {scenario.summary.stocks} stocks
                        </p>
                        <footer>
                          <span>
                            Updated{" "}
                            {new Date(scenario.created_at).toLocaleDateString()}
                          </span>
                          <Icon name="arrow" size={18} />
                        </footer>
                      </div>
                    </button>
                  ))}
                </div>
              ) : (
                <div className="ws-first-scenario">
                  <div className="ws-first-icon">
                    <Icon name="layers" size={36} />
                  </div>
                  <div>
                    <h2>
                      {search
                        ? "No scenarios match your search."
                        : "Your operation starts here."}
                    </h2>
                    <p>
                      {search
                        ? "Try another name or clear the search."
                        : "Bring a scenario file, or start with a working example below. Your original inputs stay intact as you explore alternatives."}
                    </p>
                  </div>
                  {!search && (
                    <button
                      className="ws-button"
                      onClick={() => setDialog("import")}
                    >
                      Choose a file
                      <Icon name="upload" size={16} />
                    </button>
                  )}
                </div>
              )}
              <section className="ws-starter-section">
                <div className="ws-section-heading">
                  <div>
                    <span className="ws-eyebrow">
                      START WITH SOMETHING REAL
                    </span>
                    <h2>Explore a working example.</h2>
                    <p>
                      Complete runnable models. Import one to inspect its
                      assumptions and test a baseline.
                    </p>
                  </div>
                  <span className="ws-badge">Included examples</span>
                </div>
                <div className="ws-example-grid">
                  {examples.data?.map((example, i) => (
                    <button
                      className="ws-example"
                      key={example.id}
                      disabled={load.isPending}
                      onClick={() => load.mutate(example.id)}
                    >
                      <span className={`ws-example-icon color-${i % 3}`}>
                        <Icon
                          name={
                            example.domain === "supply_chain" ? "box" : "flow"
                          }
                          size={21}
                        />
                      </span>
                      <div>
                        <strong>{example.name}</strong>
                        <small>
                          {example.domain === "supply_chain"
                            ? "Multi-tier materials, dates & evidence"
                            : example.domain === "office"
                              ? "Tasks, approvals & information flow"
                              : "Production flow & resource capacity"}
                        </small>
                      </div>
                      <Icon name="arrow" size={17} />
                    </button>
                  ))}
                </div>
              </section>
              <div className="ws-bottom-note">
                <Icon name="code" size={18} />
                <span>
                  Agents use the same scenarios, experiments, and evidence as
                  this workspace.
                </span>
                <button onClick={() => navigate("agents")}>
                  Explore agent access →
                </button>
              </div>
            </>
          )}
          {page === "experiments" && (
            <ExperimentView
              jobs={jobs.data ?? []}
              scenarios={scenarios.data ?? []}
              onCancel={(id) => cancel.mutate(id)}
            />
          )}
          {page === "build" && <BuildView />}
          {page === "agents" && <AgentView />}
          {page === "actions" && <ActionReview />}
          {(page === "data" || page === "schedule") && (
            <DecisionLab key={page} mode={page} />
          )}
        </main>
        <footer className="ws-footer">
          <span>twinflow · operational decision workspace</span>
          <span>Model → experiment → evidence</span>
        </footer>
      </div>
      {dialog && (
        <ImportDialog
          parent={dialog === "branch" ? selected : undefined}
          onClose={() => setDialog(null)}
          onImported={imported}
        />
      )}
    </div>
  );
}

function BuildView() {
  const cache = useQueryClient();
  const [name, setName] = useState("");
  const [facts, setFacts] = useState<Record<string, string>>({});
  const drafts = useQuery({
    queryKey: ["drafts"],
    queryFn: workspaceApi.drafts,
  });
  const save = useMutation({
    mutationFn: () => workspaceApi.draft(name, facts),
    onSuccess: () => {
      void cache.invalidateQueries({ queryKey: ["drafts"] });
    },
  });
  return (
    <>
      <div className="ws-page-heading">
        <div>
          <span className="ws-eyebrow">FROM INFORMATION TO A TWIN</span>
          <h1>Start with what you know.</h1>
          <p>
            Capture the facts and questions an agent needs to build your
            scenario.
          </p>
        </div>
      </div>
      <div className="ws-two-column">
        <form
          className="ws-card"
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate();
          }}
        >
          <h2>Describe the operation</h2>
          <p>
            Leave uncertain details blank. Unknown facts remain questions for
            your agent.
          </p>
          <label className="ws-field">
            Operation name
            <input
              required
              maxLength={160}
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="e.g. Purchase approval process"
            />
          </label>
          {[
            {
              id: "process",
              title: "What flows, and in what order?",
              placeholder:
                "Parts, cases, requests… describe the steps and handoffs.",
            },
            {
              id: "resources",
              title: "What limits the work?",
              placeholder: "People, skills, machines, suppliers, shared tools…",
            },
            {
              id: "demand",
              title: "What work needs to be done?",
              placeholder: "Orders, quantities, release dates, commitments…",
            },
            {
              id: "durations",
              title: "How long does the work take?",
              placeholder: "Measured times, estimates, and what varies…",
            },
            {
              id: "constraints",
              title: "What can block progress?",
              placeholder:
                "Calendars, materials, approvals, documents, quality rules…",
            },
          ].map((field) => (
            <label className="ws-field" key={field.id}>
              {field.title}
              <textarea
                rows={2}
                placeholder={field.placeholder}
                value={facts[field.id] ?? ""}
                onChange={(event) =>
                  setFacts({ ...facts, [field.id]: event.target.value })
                }
              />
            </label>
          ))}
          {save.error && <p className="ws-error">{save.error.message}</p>}
          <button className="ws-button primary" disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Save building brief"}
            <Icon name="arrow" size={16} />
          </button>
        </form>
        <aside className="ws-stack">
          <section className="ws-card ws-assumptions">
            <Icon name="spark" />
            <h2>A brief for your agent.</h2>
            <p>
              This saves your facts and identifies unanswered questions. Your
              agent translates the brief and source records into a validated
              scenario using the published schema.
            </p>
            <p>
              Saving a brief does not create a runnable twin or invent missing
              details.
            </p>
          </section>
          {drafts.data?.map((draft) => (
            <section className="ws-card" key={draft.id}>
              <div className="ws-card-heading">
                <h3>{draft.name}</h3>
                <span className="ws-badge">Draft</span>
              </div>
              <p>{draft.questions.length} questions still open</p>
              <ul className="ws-question-list">
                {draft.questions.map((question) => (
                  <li key={question.id}>{question.question}</li>
                ))}
              </ul>
              <button
                className="ws-button"
                onClick={() =>
                  download(
                    `${draft.name}-brief.json`,
                    JSON.stringify(draft, null, 2),
                  )
                }
              >
                <Icon name="down" size={16} />
                Export agent brief
              </button>
            </section>
          ))}
        </aside>
      </div>
    </>
  );
}

function AgentView() {
  const cache = useQueryClient();
  const [token, setToken] = useState("");
  const [connection, setConnection] = useState("");
  const origin = location.origin;
  const example = `# Discover the contract\ncurl ${origin}/api/workspace/capabilities\ncurl ${origin}/api/workspace/schema\n\n# Load an included example\ncurl -X POST ${origin}/api/workspace/examples/load \\\n  -H 'Content-Type: application/json' \\\n  -d '{"example_id":"spring"}'\n\n# Evaluate the returned scenario ID\ncurl -X POST ${origin}/api/workspace/scenarios/SCENARIO_ID/evaluate \\\n  -H 'Content-Type: application/json' \\\n  -d '{"reps":10,"seed":42,"request_key":"baseline-001"}'`;
  return (
    <>
      <div className="ws-page-heading">
        <div>
          <span className="ws-eyebrow">AGENT FIRST, BY DESIGN</span>
          <h1>A tool for better decisions.</h1>
          <p>
            Let your agents build the twin, resolve unknowns, and test their
            ideas against the operation.
          </p>
        </div>
        <a className="ws-button" href="/docs" target="_blank" rel="noreferrer">
          Open API reference
          <Icon name="arrow" size={16} />
        </a>
      </div>
      <form
        className="ws-card"
        onSubmit={async (event) => {
          event.preventDefault();
          setServiceToken(token);
          try {
            await workspaceApi.scenarios();
            setConnection("Connected to the workspace");
            await cache.invalidateQueries();
          } catch (error) {
            setConnection(
              error instanceof Error ? error.message : "Connection failed",
            );
          }
        }}
      >
        <h2>Workspace connection</h2>
        <p>
          If your administrator configured access control, enter the service
          token. It stays in memory for this tab.
        </p>
        <label className="ws-field">
          Service token
          <input
            type="password"
            autoComplete="off"
            value={token}
            onChange={(event) => setToken(event.target.value)}
          />
        </label>
        <button className="ws-button" type="submit">
          Connect workspace
        </button>
        <p role="status">{connection}</p>
      </form>
      <div className="ws-agent-steps">
        {[
          {
            title: "Build",
            body: "Import records, capture answers, validate the scenario.",
          },
          {
            title: "Explore",
            body: "Branch a baseline and propose explicit changes.",
          },
          {
            title: "Evaluate",
            body: "Run bounded experiments with repeatable seeds.",
          },
          {
            title: "Explain",
            body: "Retrieve results, assumptions, and evidence.",
          },
        ].map((step, i) => (
          <div key={step.title}>
            <span>0{i + 1}</span>
            <h3>{step.title}</h3>
            <p>{step.body}</p>
          </div>
        ))}
      </div>
      <div className="ws-two-column">
        <section className="ws-card">
          <div className="ws-card-heading">
            <h2>Connect through the API</h2>
            <button
              className="ws-button"
              onClick={() =>
                download("twinflow-agent-example.sh", example, "text/plain")
              }
            >
              <Icon name="down" size={16} />
              Export example
            </button>
          </div>
          <pre className="ws-source">{example}</pre>
        </section>
        <aside className="ws-stack">
          <section className="ws-card">
            <h2>One shared contract.</h2>
            <p>
              Structured inputs and outputs make Twinflow usable from any agent
              framework. The operator workspace is another client of these same
              tools.
            </p>
            <a
              className="ws-link"
              href="/api/workspace/schema"
              target="_blank"
              rel="noreferrer"
            >
              Inspect scenario schema →
            </a>
          </section>
          <section className="ws-card ws-assumptions">
            <h2>Evidence travels with the answer.</h2>
            <p>
              Scenario identity, snapshot, assumptions, replications, seed, and
              completion outcomes stay attached to each experiment.
            </p>
            <p>
              Operational changes require a separate authorized integration.
              Simulation does not write to your systems.
            </p>
          </section>
        </aside>
      </div>
    </>
  );
}
