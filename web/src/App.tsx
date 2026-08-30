import { useEffect, useState } from "react";
import { api, type ModelSummary, type ModulesResponse } from "./api";
import { FloorTab } from "./components/FloorTab";
import { RunTab } from "./components/RunTab";
import { SweepTab } from "./components/SweepTab";
import { OptimizeTab } from "./components/OptimizeTab";
import { IntegrationsTab } from "./components/IntegrationsTab";

type Tab = "floor" | "run" | "sweep" | "optimize" | "integrations";

const TABS: { id: Tab; label: string }[] = [
  { id: "floor", label: "Floor Map" },
  { id: "run", label: "Run" },
  { id: "sweep", label: "Sweep" },
  { id: "optimize", label: "Optimize" },
  { id: "integrations", label: "Integrations" },
];

function usePreferredTheme(): [string, () => void] {
  const [theme, setTheme] = useState<string>(() => {
    const stored = localStorage.getItem("twinflow-theme");
    if (stored) return stored;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  });
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("twinflow-theme", theme);
  }, [theme]);
  const toggle = () => setTheme((current) => (current === "dark" ? "light" : "dark"));
  return [theme, toggle];
}

export default function App() {
  const [theme, toggleTheme] = usePreferredTheme();
  const [tab, setTab] = useState<Tab>("floor");
  const [models, setModels] = useState<ModelSummary[]>([]);
  const [modules, setModules] = useState<ModulesResponse | null>(null);
  const [selectedModel, setSelectedModel] = useState<string>("");
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.models(), api.modules()])
      .then(([modelList, moduleList]) => {
        setModels(modelList);
        setModules(moduleList);
        if (modelList.length > 0) setSelectedModel(modelList[0].name);
      })
      .catch((error: unknown) => {
        setLoadError(error instanceof Error ? error.message : String(error));
      });
  }, []);

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark" />
          twinflow
          <span className="brand-sub">manufacturing digital twin</span>
        </div>
        <div className="header-model">
          <label htmlFor="model-select">model</label>
          <select
            id="model-select"
            value={selectedModel}
            onChange={(event) => setSelectedModel(event.target.value)}
            disabled={models.length === 0}
          >
            {models.length === 0 && <option>loading…</option>}
            {models.map((model) => (
              <option key={model.name} value={model.name} disabled={!model.has_plan}>
                {model.name}
                {!model.has_plan ? " (no plan)" : ""}
              </option>
            ))}
          </select>
          <button
            className="icon-btn theme-toggle"
            onClick={toggleTheme}
            title="Toggle light / dark"
            aria-label="Toggle theme"
          >
            {theme === "dark" ? "☀" : "☽"}
          </button>
        </div>
      </header>

      <nav className="app-nav">
        {TABS.map((item) => (
          <button
            key={item.id}
            className={`nav-item ${tab === item.id ? "active" : ""}`}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
        <div className="nav-spacer" />
      </nav>

      <main className="app-main">
        {loadError && <div className="error-banner">Could not reach the API: {loadError}</div>}
        {!loadError && !selectedModel && tab !== "integrations" && (
          <div className="empty-state">Loading models…</div>
        )}
        {selectedModel && tab === "floor" && <FloorTab model={selectedModel} />}
        {selectedModel && tab === "run" && <RunTab model={selectedModel} />}
        {selectedModel && tab === "sweep" && <SweepTab model={selectedModel} />}
        {selectedModel && tab === "optimize" && modules && (
          <OptimizeTab model={selectedModel} modules={modules} />
        )}
        {tab === "integrations" && <IntegrationsTab />}
      </main>
    </div>
  );
}
