import { Component, Suspense, lazy, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
const LegacyApp = lazy(() => import("./LegacyApp"));
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
export default function App() {
  if (new URLSearchParams(location.search).get("view") === "legacy")
    return (
      <Suspense fallback={<div>Loading simulation tools…</div>}>
        <LegacyApp />
      </Suspense>
    );
  return (
    <QueryClientProvider client={queryClient}>
      <WorkspaceBoundary>
        <WorkspaceApp />
      </WorkspaceBoundary>
    </QueryClientProvider>
  );
}
