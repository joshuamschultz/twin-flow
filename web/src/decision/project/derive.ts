// Pure helpers for the project view.
import type { ProjectViewModel } from "../types";

type Rule = ProjectViewModel["rules"][number];
type View = ProjectViewModel["views"][number];

export function ruleCounts(rules: Rule[]): {
  own: number;
  builtIn: number;
  today: string | null;
} {
  return {
    own: rules.filter((r) => r.source === "project").length,
    builtIn: rules.filter((r) => r.source === "builtin").length,
    today: rules.find((r) => r.is_baseline)?.label ?? null,
  };
}

export function viewsByKind(views: View[]): {
  stateViews: View[];
  observations: View[];
} {
  return {
    stateViews: views.filter((v) => v.kind === "state_view"),
    observations: views.filter((v) => v.kind === "observation"),
  };
}
