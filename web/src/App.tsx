import { Component, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import WorkspaceApp from "./workspace/WorkspaceApp";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, staleTime: 15000 } },
});

class WorkspaceBoundary extends Component<
  { children: ReactNode },
  { error: boolean }
> {
  state = { error: false };
  static getDerivedStateFromError() {
    return { error: true };
  }
  render() {
    return this.state.error ? (
      <main className="ws-error">
        <h1>The workspace could not render.</h1>
        <p>Your saved scenarios remain on the server.</p>
        <button onClick={() => location.reload()}>Reload workspace</button>
      </main>
    ) : (
      this.props.children
    );
  }
}
/** B-37: the classic tools are retired. Their URL opens the run or sweep list instead. */
function retireLegacyUrl(): void {
  const query = new URLSearchParams(location.search);
  if (query.get("view") !== "legacy") return;
  const tab = query.get("tab");
  query.delete("view");
  query.delete("tab");
  query.set("page", tab === "sweep" || tab === "optimize" ? "sweeps" : "runs");
  history.replaceState(history.state, "", `${location.pathname}?${query}`);
}

export default function App() {
  retireLegacyUrl();
  return (
    <QueryClientProvider client={queryClient}>
      <WorkspaceBoundary>
        <WorkspaceApp />
      </WorkspaceBoundary>
    </QueryClientProvider>
  );
}
