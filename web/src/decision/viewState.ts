// View state in the URL (DESIGN.md 2.2): the query in the workspace, the hash in the
// offline file, so a link reopens the same picture.
import {
  createContext,
  useCallback,
  useContext,
  useSyncExternalStore,
} from "react";

export type Shell = "workspace" | "offline";
export const ShellContext = createContext<Shell>("offline");

export function readParams(text: string): Record<string, string> {
  return Object.fromEntries(new URLSearchParams(text.replace(/^[#?]/, "")));
}

export function writeParam(
  text: string,
  key: string,
  value: string | null,
  mark: "#" | "?",
): string {
  const params = new URLSearchParams(text.replace(/^[#?]/, ""));
  if (value === null) params.delete(key);
  else params.set(key, value);
  const out = params.toString();
  return out ? mark + out : "";
}

const listeners = new Set<() => void>();
function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  window.addEventListener("hashchange", fn);
  window.addEventListener("popstate", fn);
  return () => {
    listeners.delete(fn);
    window.removeEventListener("hashchange", fn);
    window.removeEventListener("popstate", fn);
  };
}

function source(shell: Shell): string {
  return shell === "offline" ? window.location.hash : window.location.search;
}

export function setViewParam(
  shell: Shell,
  key: string,
  value: string | null,
): void {
  const { pathname, search, hash } = window.location;
  const next =
    shell === "offline"
      ? pathname + search + writeParam(hash, key, value, "#")
      : pathname + writeParam(search, key, value, "?") + hash;
  window.history.replaceState(window.history.state, "", next);
  listeners.forEach((fn) => fn());
}

/** One value kept in the URL. Returns the value and a setter (default removes the key). */
export function useViewState(
  key: string,
  fallback: string,
): [string, (v: string) => void] {
  const shell = useContext(ShellContext);
  const value = useSyncExternalStore(
    subscribe,
    () => readParams(source(shell))[key] ?? fallback,
    () => fallback,
  );
  const set = useCallback(
    (v: string) => setViewParam(shell, key, v === fallback ? null : v),
    [shell, key, fallback],
  );
  return [value, set];
}
